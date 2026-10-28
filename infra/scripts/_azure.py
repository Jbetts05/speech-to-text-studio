from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class CommandError(RuntimeError):
    pass


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise CommandError(f"Required command is not installed or not on PATH: {name}")


def _resolved_command(command: Sequence[str]) -> list[str]:
    if not command:
        raise CommandError("Cannot run an empty command.")
    executable = shutil.which(command[0])
    if executable is None:
        raise CommandError(f"Required command is not installed or not on PATH: {command[0]}")
    launcher = Path(executable)
    if launcher.suffix.casefold() not in {".bat", ".cmd"}:
        return [executable, *command[1:]]
    if launcher.stem.casefold() != "az":
        raise CommandError(
            f"Refusing to invoke batch launcher '{launcher.name}' because command arguments "
            "cannot be passed without shell metacharacter expansion."
        )
    candidates = (launcher.parent.parent / "python.exe", launcher.parent / "python.exe")
    python = next((candidate for candidate in candidates if candidate.is_file()), None)
    if python is None:
        raise CommandError(
            "Azure CLI resolved to a batch launcher, but its bundled Python executable "
            "could not be found."
        )
    return [str(python), "-IBm", "azure.cli", *command[1:]]


def run(
    command: Sequence[str],
    *,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    resolved_command = _resolved_command(command)
    completed = subprocess.run(
        resolved_command,
        check=False,
        capture_output=True,
        input=input_text,
        text=True,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise CommandError(f"{' '.join(command)} failed: {detail}")
    return completed


def run_json(command: Sequence[str]) -> Any:
    completed = run([*command, "--output", "json"])
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CommandError(f"{' '.join(command)} returned invalid JSON") from exc


def az_json(arguments: Sequence[str]) -> Any:
    return run_json(["az", *arguments])


def _graph_json(url: str, subscription_id: str) -> Any:
    token_record = az_json(
        [
            "account",
            "get-access-token",
            "--subscription",
            subscription_id,
            "--scope",
            "https://graph.microsoft.com/.default",
        ]
    )
    token = str(token_record.get("accessToken", "")) if isinstance(token_record, dict) else ""
    if not token:
        raise CommandError(
            "Azure CLI did not return a Microsoft Graph token for the selected subscription."
        )
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except (OSError, json.JSONDecodeError) as error:
        raise CommandError(
            "Microsoft Graph identity lookup failed for the selected subscription tenant."
        ) from error


def deployer_principal(subscription_id: str | None = None) -> tuple[str, str]:
    configured = os.environ.get("AZURE_PRINCIPAL_ID")
    if configured:
        principal_type = os.environ.get("AZURE_PRINCIPAL_TYPE")
        if principal_type not in {"User", "ServicePrincipal"}:
            raise CommandError(
                "AZURE_PRINCIPAL_TYPE must be User or ServicePrincipal when "
                "AZURE_PRINCIPAL_ID is set."
            )
        return configured, principal_type
    account_command = ["account", "show"]
    if subscription_id:
        account_command.extend(["--subscription", subscription_id])
    account = az_json(account_command)
    if not isinstance(account, dict) or not isinstance(account.get("user"), dict):
        raise CommandError("Azure CLI returned an invalid account identity record.")
    selected_subscription_id = subscription_id or str(account.get("id", ""))
    if not selected_subscription_id:
        raise CommandError("Azure CLI account record did not identify a subscription.")
    account_user = account["user"]
    principal_name = str(account_user.get("name", ""))
    principal_type = str(account_user.get("type", "")).casefold()
    if principal_type in {"serviceprincipal", "service-principal"}:
        query = urlencode(
            {
                "$filter": f"appId eq '{principal_name}'",
                "$select": "id",
            }
        )
        result = _graph_json(
            f"https://graph.microsoft.com/v1.0/servicePrincipals?{query}",
            selected_subscription_id,
        )
        values = result.get("value") if isinstance(result, dict) else None
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
            raise CommandError(
                "Microsoft Graph did not resolve exactly one service principal for "
                "the selected subscription identity."
            )
        return str(values[0]["id"]), "ServicePrincipal"
    result = _graph_json(
        "https://graph.microsoft.com/v1.0/me?%24select=id",
        selected_subscription_id,
    )
    if not isinstance(result, dict) or not result.get("id"):
        raise CommandError(
            "Microsoft Graph did not resolve the signed-in user in the selected "
            "subscription tenant."
        )
    return str(result["id"]), "User"


def deployer_principal_id(subscription_id: str | None = None) -> str:
    return deployer_principal(subscription_id)[0]


def azd_value(name: str, *, required: bool = True) -> str | None:
    completed = run(["azd", "env", "get-value", name], check=False)
    if completed.returncode != 0:
        if required:
            detail = completed.stderr.strip() or "value is not set"
            raise CommandError(f"AZD environment value {name} is unavailable: {detail}")
        return None
    return completed.stdout.strip()


def set_azd_default(name: str, value: str) -> bool:
    if azd_value(name, required=False) is not None:
        return False
    run(["azd", "env", "set", name, value])
    return True
