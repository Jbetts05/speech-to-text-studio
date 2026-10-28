"""Read-only, exact-resource preservation snapshots for an operator-reviewed release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from infra.scripts._azure import CommandError, az_json, azd_value
from infra.scripts.provider_network import public_network


def _get(url: str, subscription: str) -> dict[str, Any]:
    value = az_json(["rest", "--method", "get", "--url", url, "--subscription", subscription])
    if not isinstance(value, dict):
        raise CommandError("Invalid preservation ARM response.")
    return value


def _collection(url: str, subscription: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    while url:
        if not url.startswith("https://management.azure.com/"):
            raise CommandError("Unexpected ARM pagination host.")
        result = _get(url, subscription)
        page = result.get("value")
        if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
            raise CommandError("Invalid ARM collection in preservation snapshot.")
        values.extend(page)
        url = str(result.get("nextLink") or "")
    return values


def _configuration(resource: dict[str, Any]) -> dict[str, Any]:
    properties = {
        key: value
        for key, value in resource.get("properties", {}).items()
        if key != "provisioningState"
    }
    return {
        key: resource[key]
        for key in ("id", "name", "type", "kind", "location", "sku", "tags", "identity")
        if key in resource
    } | {"properties": properties}


def capture(subscription: str, group: str, account_scopes: tuple[str, ...]) -> dict[str, Any]:
    group_scope = f"/subscriptions/{subscription}/resourceGroups/{group}"
    group_record = az_json(["group", "show", "--name", group, "--subscription", subscription])
    accounts: dict[str, dict[str, Any]] = {}
    assignments: dict[str, dict[str, Any]] = {}
    for scope in (group_scope, *account_scopes):
        if not scope.casefold().startswith(group_scope.casefold() + "/") and scope != group_scope:
            raise CommandError("Preservation scope must stay inside the selected existing group.")
        records = az_json(
            [
                "role",
                "assignment",
                "list",
                "--scope",
                scope,
                "--include-inherited",
                "--subscription",
                subscription,
            ]
        )
        for record in records:
            assignments[str(record["id"]).casefold()] = {
                key: record.get(key)
                for key in (
                    "id",
                    "principalId",
                    "roleDefinitionId",
                    "scope",
                    "condition",
                    "conditionVersion",
                )
            }
    for scope in account_scopes:
        account = _get(f"https://management.azure.com{scope}?api-version=2025-06-01", subscription)
        accounts[scope.casefold()] = {
            "account": _configuration(account),
            **{
                kind: sorted(
                    [
                        _configuration(item)
                        for item in _collection(
                            f"https://management.azure.com{scope}/{kind}?api-version=2025-06-01",
                            subscription,
                        )
                    ],
                    key=lambda item: str(item["id"]),
                )
                for kind in ("deployments", "projects")
            },
        }
    return {
        "schema_version": "hosted-preservation-v1",
        "group_id": group_record["id"],
        "group_tags": group_record.get("tags", {}),
        "accounts": accounts,
        "role_assignments": assignments,
    }


def compare(
    before: dict[str, Any], after: dict[str, Any], *, allow_provider_public_network: bool = False
) -> None:
    for key in ("schema_version", "group_id", "group_tags"):
        if key not in before or before[key] != after.get(key):
            raise CommandError(f"Preservation failed: {key} changed or is missing.")
    expected_accounts = before.get("accounts")
    if not isinstance(expected_accounts, dict):
        raise CommandError("Preservation snapshot lacks existing accounts.")
    if allow_provider_public_network:
        expected_accounts = {
            scope: {**item, "account": public_network(item["account"])}
            for scope, item in expected_accounts.items()
        }
    if expected_accounts != after.get("accounts"):
        raise CommandError(
            "Existing accounts/projects/deployments changed beyond approved networking."
        )
    old_roles = before.get("role_assignments")
    if not isinstance(old_roles, dict):
        raise CommandError("Preservation snapshot lacks original role assignments.")
    for identity, assignment in old_roles.items():
        if after.get("role_assignments", {}).get(identity) != assignment:
            raise CommandError("An original role assignment was removed or modified.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--snapshot", type=Path)
    action.add_argument("--compare", type=Path)
    arguments = parser.parse_args()
    subscription = str(azd_value("AZURE_SUBSCRIPTION_ID"))
    group = str(azd_value("AI_BENCH_EXISTING_RESOURCE_GROUP"))
    scopes = tuple(
        f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.CognitiveServices/accounts/{azd_value(key)}"
        for key in ("AI_BENCH_FOUNDRY_ACCOUNT", "AI_BENCH_MAI_ACCOUNT")
    )
    current = capture(subscription, group, scopes)
    if arguments.snapshot:
        with arguments.snapshot.open("x", encoding="utf-8") as stream:
            json.dump(current, stream, indent=2, sort_keys=True)
            stream.write("\n")
        print("Recorded local preservation snapshot. Do not commit deployment identifiers.")
    else:
        compare(
            json.loads(arguments.compare.read_text(encoding="utf-8")),
            current,
            allow_provider_public_network=(
                azd_value("AI_BENCH_ENABLE_PROVIDER_PUBLIC_ACCESS", required=False) == "true"
            ),
        )
        print("Existing resources preserved, with only approved provider network updates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
