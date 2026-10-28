"""One explicit authenticated app-only release; default mode is read-only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from ai_bench.backend_proof import qualification_sources
from infra.scripts import preservation, stage_package
from infra.scripts._azure import CommandError, az_json, run, run_json
from infra.scripts.release_transport import Runtime

ROOT = Path(__file__).resolve().parents[2]
TERMINAL = {"completed", "completed-with-failures", "failed"}
CHECKPOINT_SETTING = "AI_BENCH_BACKEND_CONFORMANCE_RECEIPT"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CommandError(message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_: Any, **__: Any) -> urllib.request.Request | None:
        return None


def http(url: str, *, token: str | None = None) -> bytes:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    with urllib.request.build_opener(NoRedirect()).open(
        urllib.request.Request(url, headers=headers), timeout=120
    ) as response:
        return bytes(response.read())


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def authorized(
    url: str, token: str, *, method: str = "GET", etag: str | None = None
) -> urllib.request.Request:
    scheme = "Bearer"
    headers = {"Authorization": f"{scheme} {token}"}
    if etag is not None:
        headers["If-Match"] = etag
    return urllib.request.Request(url, method=method, headers=headers)


def github(arguments: list[str]) -> Any:
    return json.loads(run(["gh", *arguments]).stdout)


def json_read(url: str, *, token: str | None = None) -> Any:
    return json.loads(http(url, token=token))


def idle(url: str) -> list[dict[str, Any]]:
    records = json_read(url + "/api/runs")
    require(isinstance(records, list), "Invalid public run inventory")
    require(
        all(record["status"] in TERMINAL for record in records),
        "Active runs prevent release; wait, never cancel them",
    )
    return list(records)


def idle_after_recycle(url: str) -> list[dict[str, Any]]:
    """Only retry observation during the expected setting-triggered recycle."""
    for attempt in range(6):
        try:
            return idle(url)
        except urllib.error.HTTPError as error:
            if error.code not in {502, 503, 504} or attempt == 5:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 5:
                raise
        time.sleep(2**attempt)
    raise AssertionError("unreachable idle observation retry")


def qualification_needed(before: dict[str, Any], candidate: dict[str, Any]) -> bool:
    required = (*qualification_sources(), "requirements.lock", "infra/scripts/release_runtime.py")
    return (
        bool(candidate["qualification_required"])
        or before["python"] != candidate["python"]
        or before["versions"] != candidate["versions"]
        or any(before["files"].get(name) != candidate["files"].get(name) for name in required)
    )


def verified_ci(head: str, branch: str, repository: str) -> dict[str, Any]:
    runs = github(
        [
            "run",
            "list",
            "--repo",
            repository,
            "--branch",
            branch,
            "--workflow",
            "ci.yml",
            "--commit",
            head,
            "--limit",
            "10",
            "--json",
            "databaseId,headSha,status,conclusion,url",
        ]
    )
    require(
        bool(runs)
        and runs[0]["headSha"] == head
        and runs[0]["status"] == "completed"
        and runs[0]["conclusion"] == "success",
        "Exact accepted main-head CI must be successful",
    )
    return dict(runs[0])


def source_identity() -> dict[str, Any]:
    head = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    repository = github(["repo", "view", "--json", "nameWithOwner"])["nameWithOwner"]
    branch = github(["repo", "view", "--json", "defaultBranchRef"])["defaultBranchRef"]["name"]
    remote = github(["api", f"repos/{repository}/branches/{branch}"])["commit"]["sha"]
    require(remote == head, "Release checkout must be at accepted repository main")
    status = run(["git", "status", "--porcelain", "--untracked-files=all"]).stdout.splitlines()
    require(
        all(line[3:] == ".azure/deployment-plan.md" for line in status),
        "Unreviewed source or untracked files prevent release",
    )
    return {
        "head": head,
        "tree": run(["git", "rev-parse", "HEAD^{tree}"]).stdout.strip(),
        "repository": repository,
        "ci": verified_ci(head, branch, repository),
    }


def package(evidence: Path) -> tuple[Path, dict[str, str]]:
    run(["azd", "package", "app", "--no-prompt"])
    expected: dict[str, str] = {}
    manifest = stage_package.build_manifest()
    require(
        json.loads((stage_package.PACKAGE / "package-manifest.json").read_bytes())
        == {"files": manifest},
        "Staged manifest differs from reviewed source",
    )
    for item in manifest:
        name = str(item["path"])
        original = subprocess.check_output(["git", "show", f"HEAD:{name}"], cwd=ROOT)
        require(
            original == (stage_package.PACKAGE / name).read_bytes(),
            f"Package bytes differ from accepted Git: {name}; normalize using repository formatter",
        )
        expected[name] = sha(original)
    require(
        (stage_package.PACKAGE / "requirements.txt").read_bytes()
        == (ROOT / "requirements.lock").read_bytes(),
        "Requirements copy differs",
    )
    for name in ("requirements.txt", "package-manifest.json"):
        expected[name] = sha((stage_package.PACKAGE / name).read_bytes())
    actual = {
        path.relative_to(stage_package.PACKAGE).as_posix()
        for path in stage_package.PACKAGE.rglob("*")
        if path.is_file()
    }
    require(actual == set(expected), "Unexpected deployment package entries")
    archive = evidence / "application.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as output:
        for name in sorted(expected):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(info, (stage_package.PACKAGE / name).read_bytes())
    return archive, expected


def check_scm_delta(before: dict[str, Any], after: dict[str, Any]) -> None:
    expected = deepcopy(before)
    rules = after["scmIpSecurityRestrictions"]
    require(
        all(
            rule.get("action") == "Allow"
            and rule.get("ipAddress") == "Any"
            and not rule.get("headers")
            and not rule.get("vnetSubnetResourceId")
            for rule in rules
        ),
        "Unexpected SCM rule after Entra-only transition",
    )
    expected.update(
        scmIpSecurityRestrictions=rules,
        scmIpSecurityRestrictionsDefaultAction="Allow",
        scmIpSecurityRestrictionsUseMain=False,
    )
    require(expected == after, "Configuration changed beyond approved SCM network policy")


class Release:
    def __init__(self, evidence: Path, *, entra_only: bool, namespace: str | None) -> None:
        self.evidence, self.entra_only, self.namespace = evidence, entra_only, namespace
        self.values = run_json(["azd", "env", "get-values"])
        self.selection = [
            "--subscription",
            self.values["AZURE_SUBSCRIPTION_ID"],
            "--resource-group",
            self.values["AI_BENCH_EXISTING_RESOURCE_GROUP"],
            "--name",
            self.values["AI_BENCH_APP_NAME"],
        ]
        self.url = self.values["AI_BENCH_APP_URL"].rstrip("/")
        require(self.url.startswith("https://"), "Target must use HTTPS")
        self.app = az_json(["webapp", "show", *self.selection])
        self.id = str(self.app["id"])
        expected = (
            f"/subscriptions/{self.values['AZURE_SUBSCRIPTION_ID']}/resourceGroups/"
            f"{self.values['AI_BENCH_EXISTING_RESOURCE_GROUP']}/providers/Microsoft.Web/sites/"
            f"{self.values['AI_BENCH_APP_NAME']}"
        )
        require(self.id.casefold() == expected.casefold(), "App target identity mismatch")
        require(
            self.url == "https://" + self.app["defaultHostName"],
            "Configured URL does not belong to selected app",
        )
        self.scm = "https://" + next(
            host["name"]
            for host in self.app["hostNameSslStates"]
            if host["hostType"] == "Repository"
        )
        self.record: dict[str, Any] = {}
        self.started = False

    def kudu(self, path: str) -> Any:
        return json_read(self.scm + path, token=self.token())

    def token(self) -> str:
        return str(
            az_json(
                [
                    "account",
                    "get-access-token",
                    "--subscription",
                    self.values["AZURE_SUBSCRIPTION_ID"],
                    "--resource",
                    "https://management.azure.com/",
                ]
            )["accessToken"]
        )

    def release_orphaned_lock(self) -> dict[str, Any] | None:
        """A Kudu recycle orphans the deployment lock and rejects every later ZIP."""
        url = self.scm + "/api/vfs/site/locks/deployment/info.lock"
        opener = urllib.request.build_opener(NoRedirect())
        token = self.token()
        try:
            with opener.open(authorized(url, token), timeout=60) as response:
                etag = str(response.headers.get("ETag") or "*")
                held = json.loads(bytes(response.read()) or b"{}")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
        opener.open(authorized(url, token, method="DELETE", etag=etag), timeout=60)
        return dict(held) if isinstance(held, dict) else {"lock": held}

    def save(self, name: str, value: Any) -> None:
        (self.evidence / f"{name}.json").write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def arm(self, suffix: str) -> Any:
        return az_json(
            [
                "rest",
                "--method",
                "get",
                "--url",
                f"https://management.azure.com{self.id}{suffix}?api-version=2025-03-01",
            ]
        )

    def configuration(self) -> dict[str, Any]:
        return dict(az_json(["webapp", "config", "show", *self.selection]))

    def settings(self) -> dict[str, str]:
        return {
            item["name"]: item["value"]
            for item in az_json(["webapp", "config", "appsettings", "list", *self.selection])
        }

    def fingerprint(self) -> dict[str, str]:
        return {name: sha(value.encode()) for name, value in self.settings().items()}

    def auth_guards(self) -> None:
        require(
            all(
                self.arm(f"/basicPublishingCredentialsPolicies/{policy}")["properties"]["allow"]
                is False
                for policy in ("scm", "ftp")
            ),
            "SCM and FTP basic publishing authentication must stay disabled",
        )
        if self.entra_only:
            require(
                self.values["AI_BENCH_PUBLIC_DEMO_MODE"] == "true"
                and self.values["AI_BENCH_AUTH_MODE"] == "entra",
                "Entra-only SCM reachability requires explicitly selected Entra public demo",
            )
        plan = az_json(["appservice", "plan", "show", "--ids", self.app["serverFarmId"]])
        require(plan["sku"]["name"] == "B1" and plan["sku"]["capacity"] == 1, "Expected single B1")
        require(self.configuration()["remoteDebuggingEnabled"] is False, "Debugging must stay off")

    def scm_auth(self) -> None:
        self.kudu("/api/deployments/latest")
        try:
            http(self.scm + "/api/deployments/latest")
        except urllib.error.HTTPError as error:
            location = urlsplit(error.headers.get("Location", ""))
            require(
                error.code == 401
                or (
                    error.code in {301, 302, 303, 307, 308}
                    and location.scheme == "https"
                    and location.hostname == "login.microsoftonline.com"
                    and location.username is None
                    and location.password is None
                    and location.port in {None, 443}
                ),
                "SCM anonymous denial must be authentication, not IP rejection",
            )
        else:
            raise CommandError("Anonymous SCM deployment metadata access was accepted")

    def enable_scm(self, before: dict[str, Any], settings: dict[str, str]) -> None:
        require(self.entra_only, "SCM transition was not explicitly selected")
        require(self.configuration() == before, "SCM configuration changed during preparation")
        require(self.fingerprint() == settings, "App settings changed during preparation")
        if (
            before["scmIpSecurityRestrictionsDefaultAction"] == "Allow"
            and before["scmIpSecurityRestrictionsUseMain"] is False
        ):
            check_scm_delta(before, before)
            run(["azd", "env", "set", "AI_BENCH_SCM_ALLOWED_CIDR", ""])
            return
        idle(self.url)
        payload = {
            "properties": {
                "scmIpSecurityRestrictions": [],
                "scmIpSecurityRestrictionsDefaultAction": "Allow",
                "scmIpSecurityRestrictionsUseMain": False,
            }
        }
        self.save("scm-request", payload)
        az_json(
            [
                "rest",
                "--method",
                "patch",
                "--url",
                f"https://management.azure.com{self.id}/config/web?api-version=2025-03-01",
                "--body",
                "@" + str(self.evidence / "scm-request.json"),
            ]
        )
        after = self.configuration()
        self.save("scm-after", after)
        check_scm_delta(before, after)
        require(self.fingerprint() == settings, "SCM mutation changed app settings")
        self.auth_guards()
        # Local selected deployment policy now matches the explicitly requested ARM state.
        run(["azd", "env", "set", "AI_BENCH_SCM_ALLOWED_CIDR", ""])

    def reports(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        result = {}
        for record in records:
            if record.get("report_artifact_id") is None:
                continue
            run_id = record["run_id"]
            report = http(f"{self.url}/api/runs/{run_id}/report")
            manifest = http(f"{self.url}/api/artifacts/{record['manifest_artifact_id']}")
            require(sha(report) == record["report_artifact_id"], "Historical report hash mismatch")
            require(sha(manifest) == record["manifest_artifact_id"], "Historical manifest mismatch")
            result[run_id] = {"report": sha(report), "manifest": sha(manifest)}
        return result

    def resources(self) -> dict[str, Any]:
        app = az_json(["webapp", "show", *self.selection])
        inventory = az_json(
            [
                "resource",
                "list",
                "--subscription",
                self.values["AZURE_SUBSCRIPTION_ID"],
                "--resource-group",
                self.values["AI_BENCH_EXISTING_RESOURCE_GROUP"],
            ]
        )
        scopes = tuple(self.values[key] for key in ("AI_BENCH_FOUNDRY_SCOPE", "AI_BENCH_MAI_SCOPE"))
        return {
            "site": {
                key: app.get(key)
                for key in (
                    "id",
                    "identity",
                    "tags",
                    "serverFarmId",
                    "httpsOnly",
                    "publicNetworkAccess",
                    "virtualNetworkSubnetId",
                )
            },
            "preservation": preservation.capture(
                self.values["AZURE_SUBSCRIPTION_ID"],
                self.values["AI_BENCH_EXISTING_RESOURCE_GROUP"],
                scopes,
            ),
            "inventory": {
                resource["id"].casefold(): {
                    key: resource.get(key) for key in ("type", "name", "location", "tags")
                }
                for resource in inventory
            },
        }

    def set_checkpoint(self, digest: str, before: dict[str, str]) -> None:
        require(re.fullmatch(r"[a-f0-9]{64}", digest) is not None, "Invalid qualification digest")
        require(self.fingerprint() == before, "Concurrent app setting change")
        idle(self.url)
        # App-settings API replaces its dictionary. Preserve values in memory; only
        # the one approved setting is changed, and only fingerprints enter evidence.
        run(
            [
                "az",
                "webapp",
                "config",
                "appsettings",
                "set",
                *self.selection,
                "--settings",
                f"{CHECKPOINT_SETTING}={digest}",
                "--output",
                "none",
            ]
        )
        expected = {**before, CHECKPOINT_SETTING: sha(digest.encode())}
        actual = self.fingerprint()
        self.save("settings-after-proof", actual)
        require(actual == expected, "Unexpected app-setting delta")
        run(["azd", "env", "set", CHECKPOINT_SETTING, digest])

    def deploy_once(self, archive: Path) -> None:
        require(
            not (self.evidence / "deployment-started.json").exists(), "Deployment already attempted"
        )
        idle_after_recycle(self.url)
        previous = self.kudu("/api/deployments/latest")
        self.save(
            "deployment-started",
            {
                "previous_id": previous["id"],
                "at": datetime.now(UTC).isoformat(),
                "zip_sha256": sha(archive.read_bytes()),
            },
        )
        attempts: list[dict[str, Any]] = []
        self.record["deployment_attempts"] = attempts
        while True:
            current, result = self._send_package(archive, previous["id"])
            attempts.append(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "command_exit": result.returncode,
                    "deployment_id": current.get("id"),
                    "status": current.get("status"),
                    "complete": current.get("complete"),
                    "active": current.get("active"),
                }
            )
            self.save("kudu-after-command", current)
            self.record["deployment_command_exit"] = result.returncode
            if (
                current["id"] != previous["id"]
                and current.get("status") == 4
                and current.get("complete")
                and current.get("active")
            ):
                return
            # The identical source-bound ZIP is resent only for an interrupted
            # build that left its lock behind, never for a rejected candidate.
            released = self.release_orphaned_lock() if len(attempts) < 3 else None
            require(
                released is not None,
                "Deployment failed or ambiguous; retain state and inspect",
            )
            attempts[-1]["released_orphaned_lock"] = released
            self.save("deployment-attempts", attempts)

    def _send_package(self, archive: Path, previous_id: str) -> tuple[dict[str, Any], Any]:
        result = run(
            [
                "azd",
                "deploy",
                "app",
                "--from-package",
                str(archive),
                "--timeout",
                "2400",
                "--no-prompt",
            ],
            check=False,
        )
        log = self.evidence / f"deploy-{len(self.record['deployment_attempts']) + 1}.log"
        log.write_text(result.stdout + result.stderr, encoding="utf-8")
        deadline = time.monotonic() + 180
        while True:
            current = self.kudu("/api/deployments/latest")
            if (
                current["id"] != previous_id and current.get("complete")
            ) or time.monotonic() >= deadline:
                return dict(current), result
            time.sleep(5)

    def execute(self, *, dry_run: bool) -> dict[str, Any]:
        self.auth_guards()
        identity = source_identity()
        runs = idle(self.url)
        config, settings = self.configuration(), self.fingerprint()
        self.record = {"source": identity, "dry_run": dry_run, "provider_calls": 0}
        if dry_run:
            # No package build/hooks, SSH/SFTP, proof writes, azd env writes or ARM mutation.
            return {
                **self.record,
                "target": self.id,
                "scm_transition": self.entra_only
                and config["scmIpSecurityRestrictionsDefaultAction"] != "Allow",
                "qualification": "Determined from candidate/runtime evidence during execution",
            }
        self.evidence.mkdir(parents=True, exist_ok=False)
        self.started = True
        self.save("source", identity)
        self.save("config-before", config)
        self.save("settings-before", settings)
        resources = self.resources()
        self.save("resources-before", resources)
        reports = self.reports(runs)
        self.save("reports-before", reports)
        archive, files = package(self.evidence)
        self.save("package", {"sha256": sha(archive.read_bytes()), "files": files})
        if self.entra_only:
            self.enable_scm(config, settings)
        self.scm_auth()
        # Provider registration, quota and role preflight belong to provisioning.
        # `azd deploy` replaces application code only, so those cannot change here,
        # and verifying the build being replaced duplicates the post-deploy proof.
        remote = f"/home/data/ai-bench-release-{uuid4().hex}"
        principal = self.app["identity"]["principalId"]
        with Runtime(self.selection, principal, self.evidence) as runtime:
            before = runtime.serving("/tmp/" + remote.rsplit("/", 1)[-1])
            require(not before["active_runs"], "Active runs prevent candidate preparation")
            require(
                before["files"].get("requirements.lock") == files["requirements.lock"],
                "Dependency-lock migration needs a separately matched candidate environment",
            )
            candidate_root = runtime.stage(archive, remote, files)
            candidate = runtime.candidate("inspect", candidate_root, remote)
            require(
                candidate["files"]
                == {
                    k: v
                    for k, v in files.items()
                    if k not in {"package-manifest.json", "requirements.txt"}
                },
                "Candidate source identity differs",
            )
            self.save("candidate", candidate)
            self.save("serving-before", before)
            require(
                before["python"] == candidate["python"]
                and before["versions"] == candidate["versions"],
                "Candidate must reproduce actual worker dependency precedence",
            )
            require(
                before["configuration_signature"] == candidate["configuration_signature"],
                "Candidate storage configuration differs from serving worker",
            )
            need = qualification_needed(before, candidate)
            digest = self.values.get(CHECKPOINT_SETTING, "")
            if need:
                require(
                    self.namespace is not None
                    and re.fullmatch(r"conformance-[A-Za-z0-9_-]+", self.namespace) is not None,
                    "Backend change requires an explicitly approved fresh conformance namespace",
                )
                proof = runtime.candidate(
                    "qualify", candidate_root, remote, namespace=self.namespace
                )
                require(
                    proof["code_signature"] == candidate["code_signature"],
                    "Qualification signature changed during execution",
                )
                digest = proof["qualification"]["receipt_digest"]
                self.save("qualification", proof)
            self.record["qualification_performed"] = need
            require(source_identity() == identity, "Accepted source/CI changed before cutover")
            require(sha(archive.read_bytes()) == self._package_hash(), "Frozen ZIP changed")
            require(self.fingerprint() == settings, "Settings changed during qualification")
            if self.entra_only:
                check_scm_delta(config, self.configuration())
            else:
                require(self.configuration() == config, "Configuration changed before cutover")
            if candidate["qualification_required"]:
                self.set_checkpoint(digest, settings)
                settings = {**settings, CHECKPOINT_SETTING: sha(digest.encode())}
            self.deploy_once(archive)
        # Deployment may rotate SSH host/interpreter. Establish a fresh authenticated tunnel.
        with Runtime(self.selection, principal, self.evidence) as runtime:
            served = runtime.serving("/tmp/" + remote.rsplit("/", 1)[-1] + "-after")
            require(served["files"] == candidate["files"], "Serving bytes differ from candidate")
            require(
                served["python"] == candidate["python"]
                and served["versions"] == candidate["versions"],
                "Serving dependencies differ from qualified candidate; forward repair required",
            )
            require(
                served["code_signature"] == candidate["code_signature"]
                and served["configuration_signature"] == candidate["configuration_signature"],
                "Serving backend provenance/configuration differs from qualified candidate",
            )
            self.save("serving-after", served)
            self.record["active_runs_after"] = served["active_runs"]
        run([sys.executable, "-m", "infra.scripts.verify_deployment"])
        require(self.reports(runs) == reports, "Original reports or manifests changed")
        require(self.fingerprint() == settings, "Unexpected final app settings change")
        if self.entra_only:
            check_scm_delta(config, self.configuration())
        else:
            require(self.configuration() == config, "Unexpected final configuration change")
        after_resources = self.resources()
        self.save("resources-after", after_resources)
        preservation.compare(resources["preservation"], after_resources["preservation"])
        require(after_resources["inventory"] == resources["inventory"], "Resource inventory drift")
        require(after_resources["site"] == resources["site"], "Site identity/topology drift")
        self.save("config-after", self.configuration())
        self.save("settings-after", self.fingerprint())
        self.save("reports-after", reports)
        self.record["status"] = "verified"
        self.save("release", self.record)
        return self.record

    def _package_hash(self) -> str:
        return str(json.loads((self.evidence / "package.json").read_bytes())["sha256"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--scm-entra-only", action="store_true")
    parser.add_argument("--conformance-namespace")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    require(Path.cwd().resolve() == ROOT, "Run from this repository root to select its AZD context")
    require(sys.prefix != sys.base_prefix, "Use an isolated operator virtual environment")
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    evidence = args.evidence_dir.resolve()
    require(not evidence.is_relative_to(ROOT), "Keep release evidence outside the checkout")
    operation = Release(
        evidence, entra_only=args.scm_entra_only, namespace=args.conformance_namespace
    )
    try:
        result = operation.execute(dry_run=not args.execute)
    except Exception as error:
        if args.execute and operation.started:
            operation.save(
                "failure",
                {
                    "type": type(error).__name__,
                    "message": str(error),
                    "no_automatic_retry": True,
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        raise
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
