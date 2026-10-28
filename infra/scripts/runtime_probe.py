"""Operator-only MI and private-storage/public-provider connectivity proof, never Kudu."""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import socket
import ssl
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def worker_identity(worker_pid: int) -> dict[str, str]:
    if worker_pid <= 0 or os.getenv("KUDU_BUILD_VERSION"):
        raise ValueError("Run this command inside the app runtime, never the SCM container.")
    process = Path("/proc") / str(worker_pid)
    command = process.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode()
    if "ai_bench.api:create_app" not in command or "uvicorn" not in command:
        raise ValueError("Selected PID is not the deployed AI Bench API/worker process.")
    environment = dict(
        part.decode().split("=", 1)
        for part in process.joinpath("environ").read_bytes().split(b"\0")
        if b"=" in part
    )
    names = ("WEBSITE_INSTANCE_ID", "IDENTITY_ENDPOINT", "AI_BENCH_APP_PRINCIPAL_ID")
    if any(not os.getenv(name) or environment.get(name) != os.getenv(name) for name in names):
        raise ValueError(
            "Probe must share the actual worker instance and managed-identity endpoint."
        )
    if environment.get("AI_BENCH_EXECUTION_BACKEND") != "azure-table-blob":
        raise ValueError("Worker is not configured for Azure Table/Blob execution.")
    return {"worker_pid": str(worker_pid), "instance": environment["WEBSITE_INSTANCE_ID"]}


def private_connectivity(endpoint: str, *, private_required: bool = True) -> dict[str, Any]:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only configured HTTPS resource endpoints are accepted.")
    if parsed.port not in {None, 443} or parsed.query or parsed.fragment:
        raise ValueError("Unexpected configured endpoint port, query or fragment.")
    addresses = sorted(
        {
            str(item[4][0])
            for item in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        }
    )
    private_ranges = tuple(
        ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    )
    if private_required:
        if not addresses or any(
            not any(ipaddress.ip_address(address) in network for network in private_ranges)
            for address in addresses
        ):
            raise ValueError("Storage DNS must resolve exclusively to private VNet addresses.")
    elif not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Provider DNS must resolve publicly; remove stale AI private DNS routing.")
    for address in addresses:
        with (
            socket.create_connection((address, 443), timeout=20) as connection,
            ssl.create_default_context().wrap_socket(connection, server_hostname=parsed.hostname),
        ):
            pass
    return {
        "host": parsed.hostname,
        "addresses": addresses,
        "tls_verified": True,
        "network": "private" if private_required else "public",
    }


def run_probe(worker_pid: int, provider_settings: list[str]) -> dict[str, Any]:
    identity = worker_identity(worker_pid)
    if len(provider_settings) < 2 or any(
        not name.startswith("AI_BENCH_") or not name.endswith("_ENDPOINT")
        for name in provider_settings
    ):
        raise ValueError("Explicitly select the existing providers' endpoint settings.")
    storage_settings = [
        "AI_BENCH_AZURE_STORAGE_ACCOUNT_URL",
        "AI_BENCH_AZURE_TABLE_ENDPOINT",
    ]
    connectivity = {name: private_connectivity(os.environ[name]) for name in storage_settings} | {
        name: private_connectivity(os.environ[name], private_required=False)
        for name in provider_settings
    }
    from azure.data.tables import TableClient
    from azure.identity import ManagedIdentityCredential
    from azure.storage.blob import ContainerClient

    with ManagedIdentityCredential() as credential:
        for audience in (
            "https://storage.azure.com/.default",
            "https://cognitiveservices.azure.com/.default",
        ):
            token = credential.get_token(audience)
            claims_segment = token.token.split(".")[1]
            claims = json.loads(
                base64.urlsafe_b64decode(claims_segment + "=" * (-len(claims_segment) % 4))
            )
            if (
                str(claims.get("oid", "")).casefold()
                != os.environ["AI_BENCH_APP_PRINCIPAL_ID"].casefold()
            ):
                raise ValueError(
                    "Managed identity token does not belong to the configured app principal."
                )
        for setting in ("AI_BENCH_AZURE_ARTIFACT_CONTAINER", "AI_BENCH_AZURE_STAGING_CONTAINER"):
            with ContainerClient(
                os.environ["AI_BENCH_AZURE_STORAGE_ACCOUNT_URL"],
                os.environ[setting],
                credential=credential,
                retry_total=0,
            ) as container:
                container.get_container_properties()
        with TableClient(
            os.environ["AI_BENCH_AZURE_TABLE_ENDPOINT"],
            os.environ["AI_BENCH_AZURE_STATE_TABLE"],
            credential=credential,
            retry_total=0,
        ) as table:
            next(
                iter(
                    table.query_entities(
                        "PartitionKey eq '__connectivity_probe__'", results_per_page=1
                    )
                ),
                None,
            )
    return {
        "schema_version": "worker-connectivity-v2",
        "recorded_at": datetime.now(UTC).isoformat(),
        "worker": identity,
        "credential": "ManagedIdentityCredential",
        "connectivity": connectivity,
        "blob_table_read": True,
        "provider_call_performed": False,
        "conformance_proven": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-pid", type=int, required=True)
    parser.add_argument("--provider-setting", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    evidence = run_probe(arguments.worker_pid, arguments.provider_setting)
    with arguments.output.open("x", encoding="utf-8") as stream:
        json.dump(evidence, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(
        "Worker MI, private storage/public provider DNS/TLS and storage reads verified; "
        "no provider inference request made."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
