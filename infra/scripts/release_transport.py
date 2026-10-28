"""Owned Entra App Service tunnel and worker-bound operator subprocess transport."""

from __future__ import annotations

import base64
import importlib
import json
import shlex
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from infra.scripts._azure import CommandError, _resolved_command

BOOTSTRAP = r"""
import base64,json,os,pathlib,subprocess,sys
request=json.loads(base64.b64decode(sys.argv[1]))
workers=[]
for path in pathlib.Path("/proc").iterdir():
    if not path.name.isdigit(): continue
    try:
        command=path.joinpath("cmdline").read_bytes().split(b"\0")
        if b"uvicorn" in command and b"ai_bench.api:create_app" in command: workers.append(path)
    except (FileNotFoundError,PermissionError,ProcessLookupError): continue
if len(workers)!=1: raise RuntimeError("Expected exactly one serving worker")
worker=workers[0]
env=dict(part.decode().split("=",1) for part in worker.joinpath("environ").read_bytes().split(b"\0")
         if b"=" in part)
if env.get("AI_BENCH_RUNTIME")!="azure" or env.get("KUDU_BUILD_VERSION"):
    raise RuntimeError("Not the actual application runtime")
if env.get("AI_BENCH_APP_PRINCIPAL_ID","").lower()!=request["principal"].lower():
    raise RuntimeError("Worker principal does not match ARM")
cwd=worker.joinpath("cwd").resolve()
venv=pathlib.Path(env["VIRTUAL_ENV"])
python=venv/"bin/python"
if not python.is_file(): raise RuntimeError("Worker interpreter missing")
root=pathlib.Path(request.get("root",str(cwd)))
env["AI_BENCH_ALLOW_NETWORKED_SOLUTIONS"]="false"
env["PYTHONDONTWRITEBYTECODE"]="1"
env["PYTHONPATH"]=str(root/"src")+":"+str(root)+":"+env.get("PYTHONPATH","")
args=[str(value).replace("{worker_pid}",worker.name).replace("{root}",str(root))
      for value in request["arguments"]]
raise SystemExit(subprocess.run([str(python),*args],cwd=root,env=env).returncode)
"""

SERVING = r"""
import hashlib,importlib.metadata,json,os,pathlib,sys
from ai_bench.backends import azure_backend
from ai_bench.backend_proof import backend_code_signature
from infra.scripts.runtime_probe import run_probe
root=pathlib.Path.cwd()
manifest=json.loads((root/"package-manifest.json").read_bytes())
files={}
for item in manifest["files"]:
    path=pathlib.Path(item["path"])
    if path.is_absolute() or ".." in path.parts: raise ValueError("Unsafe serving path")
    data=(root/path).read_bytes()
    if len(data)!=item["bytes"] or hashlib.sha256(data).hexdigest()!=item["sha256"]:
        raise ValueError("Serving package integrity failure")
    files[item["path"]]=item["sha256"]
backend=azure_backend(pathlib.Path(sys.argv[2])/"serving-cache")
backend.assert_ready()
active=[run.manifest.run_id for run in backend.repository.unfinished_runs()]
names=("azure-core","azure-data-tables","azure-storage-blob","azure-identity","pydantic")
print(json.dumps({"root":str(root),"files":files,"python":list(sys.version_info[:2]),
    "versions":{name:importlib.metadata.version(name) for name in names},
    "active_runs":active,
    "code_signature":backend_code_signature(),"configuration_signature":backend.configuration_signature,
    "digest":os.environ.get("AI_BENCH_BACKEND_CONFORMANCE_RECEIPT",""),
    "connectivity":run_probe(int(sys.argv[1]),["AI_BENCH_SPEECH_ENDPOINT",
      "AI_BENCH_OPENAI_ENDPOINT","AI_BENCH_MAI_ENDPOINT"])}))
"""


class Runtime:
    def __init__(self, selection: list[str], principal: str, evidence: Path) -> None:
        self.selection, self.principal, self.evidence = selection, principal, evidence
        self.process: subprocess.Popen[bytes] | None = None
        self.client: Any = None
        self.log: Any = None

    def __enter__(self) -> Runtime:
        try:
            paramiko = importlib.import_module("paramiko")
        except ImportError as error:
            raise CommandError(
                "Install requirements-operator.lock in the active virtual environment."
            ) from error
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.log = (self.evidence / "tunnel.log").open("ab")
        self.process = subprocess.Popen(
            _resolved_command(
                ["az", "webapp", "create-remote-connection", *self.selection, "--port", str(port)]
            ),
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 60
            while True:
                try:
                    connection = socket.create_connection(("127.0.0.1", port), timeout=3)
                    break
                except OSError:
                    if self.process.poll() is not None or time.monotonic() >= deadline:
                        raise CommandError("Authenticated runtime tunnel unavailable") from None
                    time.sleep(1)
            with connection:
                transport = paramiko.Transport(connection)
                try:
                    transport.start_client(timeout=15)
                    key = transport.get_remote_server_key()
                finally:
                    transport.close()
            (self.evidence / "host-key.json").write_text(
                json.dumps(
                    {
                        "type": key.get_name(),
                        "key": key.get_base64(),
                        "basis": "new Entra-authenticated exact-app tunnel",
                    }
                )
            )
            self.client = paramiko.SSHClient()
            self.client.get_host_keys().add(f"[127.0.0.1]:{port}", key.get_name(), key)
            # Built-in container default, never publishing credentials; Entra protects the tunnel.
            self.client.connect(
                "127.0.0.1",
                port=port,
                username="root",
                password="Docker!",
                look_for_keys=False,
                allow_agent=False,
                timeout=30,
            )
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_: object) -> None:
        if self.client is not None:
            self.client.close()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=15)
        if self.log is not None:
            self.log.close()

    def run(self, name: str, arguments: list[str], *, root: str | None = None) -> dict[str, Any]:
        request: dict[str, Any] = {"principal": self.principal, "arguments": arguments}
        if root is not None:
            request["root"] = root
        encoded = base64.b64encode(json.dumps(request).encode()).decode()
        _, output, _ = self.client.exec_command(
            "python -c " + shlex.quote(BOOTSTRAP) + " " + shlex.quote(encoded), timeout=3600
        )
        output.channel.set_combine_stderr(True)
        result: dict[str, Any] | None = None
        with (self.evidence / f"{name}.log").open("a", encoding="utf-8") as log:
            for line in output:
                log.write(line)
                log.flush()
                if line.startswith("{"):
                    result = json.loads(line)
        if output.channel.recv_exit_status() != 0 or result is None:
            raise CommandError(
                f"Runtime {name} failed; inspect retained evidence; do not retry writes"
            )
        return result

    def serving(self, remote: str) -> dict[str, Any]:
        return self.run("serving", ["-c", SERVING, "{worker_pid}", remote])

    def stage(self, archive: Path, remote: str, expected: dict[str, str]) -> str:
        with self.client.open_sftp() as sftp:
            # mkdir fails on an existing operation directory; never overwrite or clear.
            sftp.mkdir(remote)
            sftp.put(str(archive), remote + "/candidate.zip")
        code = (
            "import hashlib,json,pathlib,zipfile;"
            f"root=pathlib.Path({remote!r});expected={expected!r};"
            "target=root/'candidate';target.mkdir();"
            "archive=zipfile.ZipFile(root/'candidate.zip');"
            "assert len(archive.namelist())==len(set(archive.namelist()));"
            "assert set(archive.namelist())==set(expected)\n"
            "for name,digest in expected.items():\n"
            " path=pathlib.PurePosixPath(name)\n"
            " assert not path.is_absolute() and '..' not in path.parts\n"
            " data=archive.read(name);assert hashlib.sha256(data).hexdigest()==digest\n"
            " dest=target.joinpath(*path.parts);dest.parent.mkdir(parents=True,exist_ok=True)\n"
            " with dest.open('xb') as stream:stream.write(data)\n"
            "print(json.dumps({'staged':str(target),'files':len(expected)}))"
        )
        self.run("stage", ["-c", code])
        return remote + "/candidate"

    def candidate(
        self,
        action: str,
        root: str,
        remote: str,
        *,
        namespace: str | None = None,
        digest: str | None = None,
    ) -> dict[str, Any]:
        arguments = [
            "-m",
            "infra.scripts.release_runtime",
            action,
            "--root",
            root,
            "--state-root",
            remote + "/state",
            "--worker-pid",
            "{worker_pid}",
        ]
        if namespace:
            arguments.extend(("--namespace", namespace))
        if digest:
            arguments.extend(("--digest", digest))
        return self.run(action, arguments, root=root)
