from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from infra.scripts._azure import (
    CommandError,
    az_json,
    azd_value,
    deployer_principal,
    require_command,
    run,
)

KEY_VAULT_SECRETS_USER = "Key Vault Secrets User"
KEY_VAULT_SECRETS_OFFICER = "Key Vault Secrets Officer"
COGNITIVE_SERVICES_USER = "Cognitive Services User"
SECRET_NAME = "ai-bench-foundry-api-key"
MODE_STATE_NAME = "AI_BENCH_KEY_VAULT_MODE_STATE"
PRIVATE_ACCESS_NAME = "AI_BENCH_OPERATOR_HAS_PRIVATE_KEY_VAULT_ACCESS"


def _subscription_id(scope: str) -> str:
    matched = re.match(r"^/subscriptions/([^/]+)(?:/|$)", scope, flags=re.IGNORECASE)
    if matched is None:
        raise CommandError(f"Cannot determine subscription from Azure scope: {scope}")
    return matched.group(1)


def _role_assignments(
    principal_id: str,
    scope: str,
    role_name: str,
) -> list[dict[str, Any]]:
    return list(
        az_json(
            [
                "role",
                "assignment",
                "list",
                "--assignee-object-id",
                principal_id,
                "--scope",
                scope,
                "--role",
                role_name,
                "--subscription",
                _subscription_id(scope),
            ]
        )
    )


def _delete_role(principal_id: str, scope: str, role_name: str) -> None:
    owned_name = _owned_assignment_name(principal_id, scope, role_name)
    for assignment in _role_assignments(principal_id, scope, role_name):
        assignment_id = str(assignment["id"])
        expected_id = (
            f"{scope.rstrip('/')}/providers/Microsoft.Authorization/roleAssignments/{owned_name}"
        )
        if assignment_id.casefold() != expected_id.casefold():
            continue
        run(
            [
                "az",
                "role",
                "assignment",
                "delete",
                "--ids",
                assignment_id,
                "--subscription",
                _subscription_id(scope),
            ]
        )


def _owned_assignment_name(principal_id: str, scope: str, role_name: str) -> str:
    identity = "|".join((scope.casefold(), principal_id.casefold(), role_name.casefold()))
    return str(uuid5(NAMESPACE_URL, f"ai-bench-temporary-role:{identity}"))


def _delete_assignment(
    assignment_id: str,
    scope: str,
    *,
    missing_ok: bool = False,
) -> None:
    result = run(
        [
            "az",
            "role",
            "assignment",
            "delete",
            "--ids",
            assignment_id,
            "--subscription",
            _subscription_id(scope),
        ],
        check=False,
    )
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout).strip()
    folded = detail.casefold()
    if missing_ok and (
        "does not exist" in folded or "could not be found" in folded or "not found" in folded
    ):
        return
    raise CommandError(f"Unable to delete temporary role assignment: {detail}")


def _delete_owned_assignment(
    principal_id: str,
    scope: str,
    role_name: str,
) -> None:
    assignment_name = _owned_assignment_name(principal_id, scope, role_name)
    assignment_id = (
        f"{scope.rstrip('/')}/providers/Microsoft.Authorization/roleAssignments/{assignment_name}"
    )
    _delete_assignment(assignment_id, scope, missing_ok=True)


def _ensure_role(
    principal_id: str,
    principal_type: str,
    scope: str,
    role_name: str,
) -> bool:
    assignment_name = _owned_assignment_name(principal_id, scope, role_name)
    owned_assignment_id = (
        f"{scope.rstrip('/')}/providers/Microsoft.Authorization/roleAssignments/{assignment_name}"
    )
    owns_assignment = False

    def cleanup_created_assignment() -> None:
        if owns_assignment:
            _delete_assignment(owned_assignment_id, scope, missing_ok=True)

    try:
        assignments = _role_assignments(principal_id, scope, role_name)
        owns_assignment = any(
            str(assignment.get("id", "")).casefold() == owned_assignment_id.casefold()
            for assignment in assignments
        )
        if assignments and not owns_assignment:
            return False
        if not owns_assignment:
            owns_assignment = True
            run(
                [
                    "az",
                    "role",
                    "assignment",
                    "create",
                    "--assignee-object-id",
                    principal_id,
                    "--assignee-principal-type",
                    principal_type,
                    "--scope",
                    scope,
                    "--role",
                    role_name,
                    "--name",
                    str(assignment_name),
                    "--subscription",
                    _subscription_id(scope),
                    "--output",
                    "json",
                ]
            )
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if any(
                str(assignment.get("id", "")).casefold() == owned_assignment_id.casefold()
                for assignment in _role_assignments(principal_id, scope, role_name)
            ):
                return owns_assignment
            time.sleep(5)
    except (Exception, KeyboardInterrupt):
        cleanup_created_assignment()
        raise
    cleanup_created_assignment()
    raise CommandError(f"Timed out waiting for {role_name} propagation at {scope}.")


def _secret_exists(vault_name: str, subscription_id: str) -> bool:
    secrets = az_json(
        [
            "keyvault",
            "secret",
            "list",
            "--vault-name",
            vault_name,
            "--subscription",
            subscription_id,
        ]
    )
    return any(str(secret.get("name", "")) == SECRET_NAME for secret in secrets)


def _secret_deleted(vault_name: str, subscription_id: str) -> bool:
    deleted = az_json(
        [
            "keyvault",
            "secret",
            "list-deleted",
            "--vault-name",
            vault_name,
            "--subscription",
            subscription_id,
        ]
    )
    return any(str(secret.get("name", "")) == SECRET_NAME for secret in deleted)


def _remove_secret_without_reading(vault_name: str, subscription_id: str) -> None:
    result = run(
        [
            "az",
            "keyvault",
            "secret",
            "delete",
            "--vault-name",
            vault_name,
            "--name",
            SECRET_NAME,
            "--subscription",
            subscription_id,
            "--only-show-errors",
            "--output",
            "none",
        ],
        check=False,
    )
    if result.returncode == 0:
        return
    detail = result.stderr.strip() or result.stdout.strip()
    normalized_detail = detail.casefold()
    if (
        "resourcenotfound" in normalized_detail
        or "secretnotfound" in normalized_detail
        or "not found" in normalized_detail
    ):
        return
    raise CommandError(
        "Failed to remove the stale provider secret through the Key Vault data plane. "
        "Run from a host with private DNS and network access to the vault endpoint. "
        f"Azure CLI detail: {detail or 'unknown error'}"
    )


def _wait_for_secret_access(vault_name: str, subscription_id: str) -> None:
    deadline = time.monotonic() + 180
    last_error: CommandError | None = None
    while time.monotonic() < deadline:
        try:
            az_json(
                [
                    "keyvault",
                    "secret",
                    "list",
                    "--vault-name",
                    vault_name,
                    "--subscription",
                    subscription_id,
                ]
            )
            return
        except CommandError as error:
            last_error = error
        time.sleep(5)
    raise CommandError(f"Timed out waiting for Key Vault data-plane access: {last_error}")


def _recover_secret(vault_name: str, subscription_id: str) -> None:
    if not _secret_deleted(vault_name, subscription_id):
        return
    run(
        [
            "az",
            "keyvault",
            "secret",
            "recover",
            "--vault-name",
            vault_name,
            "--name",
            SECRET_NAME,
            "--subscription",
            subscription_id,
        ]
    )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if _secret_exists(vault_name, subscription_id):
            return
        time.sleep(5)
    raise CommandError(f"Timed out recovering secret {SECRET_NAME}.")


def _write_secret(vault_name: str, value: str, subscription_id: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=Path.cwd(),
        ) as temporary:
            temporary.write(value)
            temporary.flush()
            os.chmod(temporary.name, 0o600)
            temporary_path = Path(temporary.name)
        run(
            [
                "az",
                "keyvault",
                "secret",
                "set",
                "--vault-name",
                vault_name,
                "--name",
                SECRET_NAME,
                "--file",
                str(temporary_path),
                "--subscription",
                subscription_id,
            ]
        )
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _remove_key_setting(
    subscription_id: str,
    resource_group: str,
    app_name: str,
) -> None:
    run(
        [
            "az",
            "webapp",
            "config",
            "appsettings",
            "delete",
            "--resource-group",
            resource_group,
            "--name",
            app_name,
            "--setting-names",
            "AI_BENCH_FOUNDRY_API_KEY",
            "--subscription",
            subscription_id,
        ]
    )


def _set_key_reference(
    subscription_id: str,
    resource_group: str,
    app_name: str,
    vault_name: str,
) -> None:
    reference = f"@Microsoft.KeyVault(VaultName={vault_name};SecretName={SECRET_NAME})"
    run(
        [
            "az",
            "webapp",
            "config",
            "appsettings",
            "set",
            "--resource-group",
            resource_group,
            "--name",
            app_name,
            "--settings",
            f"AI_BENCH_FOUNDRY_API_KEY={reference}",
            "--subscription",
            subscription_id,
        ]
    )


def _set_mode_state(value: str) -> None:
    run(["azd", "env", "set", MODE_STATE_NAME, value])


def _requires_secret_cleanup(mode_state: str) -> bool:
    if mode_state not in {"fresh", "clean", "key", "key-pending", "unknown"}:
        raise CommandError(f"{MODE_STATE_NAME} has an invalid value: {mode_state}")
    return mode_state in {"key", "key-pending", "unknown"}


def main() -> int:
    require_command("az")
    require_command("azd")

    subscription_id = str(azd_value("AZURE_SUBSCRIPTION_ID"))
    mode = str(azd_value("AI_BENCH_AUTH_MODE")).casefold()
    mode_state = str(azd_value(MODE_STATE_NAME, required=False) or "unknown").casefold()
    operator_private_access = str(
        azd_value(PRIVATE_ACCESS_NAME, required=False) or "false"
    ).casefold()
    resource_group = str(azd_value("AZURE_RESOURCE_GROUP"))
    app_name = str(azd_value("AI_BENCH_APP_NAME"))
    principal_id = str(azd_value("AI_BENCH_APP_PRINCIPAL_ID"))
    vault_name = str(azd_value("AI_BENCH_KEY_VAULT_NAME"))
    foundry_scope = str(azd_value("AI_BENCH_FOUNDRY_SCOPE") or "")
    if azd_value("AI_BENCH_PUBLIC_DEMO_MODE", required=False) == "true" and mode != "entra":
        raise CommandError("Public hosted demo requires Entra mode.")
    vault = az_json(
        [
            "keyvault",
            "show",
            "--resource-group",
            resource_group,
            "--name",
            vault_name,
            "--subscription",
            subscription_id,
        ]
    )
    vault_scope = str(vault["id"])
    deployer_id, deployer_type = deployer_principal(subscription_id)

    if mode in {"offline", "entra"}:
        if _requires_secret_cleanup(mode_state):
            if operator_private_access != "true":
                raise CommandError(
                    "Removing stale key-mode state requires private Key Vault access. "
                    f"Verify connectivity and set {PRIVATE_ACCESS_NAME}=true."
                )
            temporary_role = _ensure_role(
                deployer_id,
                deployer_type,
                vault_scope,
                KEY_VAULT_SECRETS_OFFICER,
            )
            try:
                _wait_for_secret_access(vault_name, subscription_id)
                _remove_secret_without_reading(vault_name, subscription_id)
            finally:
                if temporary_role:
                    _delete_owned_assignment(
                        deployer_id,
                        vault_scope,
                        KEY_VAULT_SECRETS_OFFICER,
                    )
        _remove_key_setting(subscription_id, resource_group, app_name)
        _delete_role(principal_id, vault_scope, KEY_VAULT_SECRETS_USER)
        if mode == "offline" and foundry_scope:
            _delete_role(principal_id, foundry_scope, COGNITIVE_SERVICES_USER)
        if mode == "entra" and foundry_scope:
            _ensure_role(
                principal_id,
                "ServicePrincipal",
                foundry_scope,
                COGNITIVE_SERVICES_USER,
            )
        _set_mode_state("clean")
        print(f"Synchronized {mode} mode without reading or retaining a provider key.")
        return 0

    if mode != "key":
        raise CommandError("AI_BENCH_AUTH_MODE must be offline, entra, or key.")

    key = os.environ.pop("AI_BENCH_FOUNDRY_API_KEY", None)
    if not key:
        raise CommandError(
            "Key mode requires AI_BENCH_FOUNDRY_API_KEY in the current process "
            "environment. It is never read from or written to AZD environment state."
        )
    if not foundry_scope:
        raise CommandError("Key mode requires a configured Foundry account scope.")
    if operator_private_access != "true":
        raise CommandError(
            "Key mode requires private Key Vault access. Verify connectivity and set "
            f"{PRIVATE_ACCESS_NAME}=true."
        )

    _set_mode_state("key-pending")
    _delete_role(principal_id, foundry_scope, COGNITIVE_SERVICES_USER)
    _ensure_role(
        principal_id,
        "ServicePrincipal",
        vault_scope,
        KEY_VAULT_SECRETS_USER,
    )
    temporary_role = _ensure_role(
        deployer_id,
        deployer_type,
        vault_scope,
        KEY_VAULT_SECRETS_OFFICER,
    )
    try:
        _wait_for_secret_access(vault_name, subscription_id)
        _recover_secret(vault_name, subscription_id)
        _write_secret(vault_name, key, subscription_id)
    finally:
        if temporary_role:
            _delete_owned_assignment(
                deployer_id,
                vault_scope,
                KEY_VAULT_SECRETS_OFFICER,
            )
    _set_key_reference(subscription_id, resource_group, app_name, vault_name)
    _set_mode_state("key")
    print(
        "Synchronized explicit key mode. The supplied value was written through a "
        "temporary restricted file and was not persisted in AZD environment state."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
