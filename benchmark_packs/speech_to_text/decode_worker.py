"""Stable module entrypoint for one resource-bounded upload validation process."""

from __future__ import annotations

import json
import sys
from pathlib import Path

MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
CPU_LIMIT_SECONDS = 90
_WINDOWS_JOB: object = None


def apply_resource_limits() -> None:
    if sys.platform == "win32":
        _windows_limits()
    else:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_SECONDS, CPU_LIMIT_SECONDS))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))


def _windows_limits() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows decoder job limits require Windows.")

    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = (
            ("process_time", ctypes.c_longlong),
            ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD),
            ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t),
            ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        )

    class IoCounters(ctypes.Structure):
        _fields_ = tuple(
            (name, ctypes.c_ulonglong)
            for name in ("read_count", "write_count", "other_count", "read", "write", "other")
        )

    class ExtendedLimits(ctypes.Structure):
        _fields_ = (
            ("basic", BasicLimits),
            ("io", IoCounters),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t),
            ("peak_job_memory", ctypes.c_size_t),
        )

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise RuntimeError("Decoder memory limits could not be established.")
    limits = ExtendedLimits()
    limits.basic.flags = 0x100 | 0x2
    limits.basic.process_time = CPU_LIMIT_SECONDS * 10_000_000
    limits.process_memory = MEMORY_LIMIT_BYTES
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        kernel.CloseHandle(job)
        raise RuntimeError("Decoder memory limits could not be established.")
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        kernel.CloseHandle(job)
        raise RuntimeError("Decoder memory limits could not be established.")
    global _WINDOWS_JOB
    _WINDOWS_JOB = job


def main() -> int:
    try:
        apply_resource_limits()
        from ai_bench.contracts import SampleSnapshot
        from ai_bench.dataset_imports import DatasetImportFile
        from ai_bench.hashing import canonical_json

        from .decode_process import MAX_WORKER_REQUEST_BYTES, MAX_WORKER_RESPONSE_BYTES
        from .upload import SpeechDatasetImporter

        raw = sys.stdin.buffer.read(MAX_WORKER_REQUEST_BYTES + 1)
        if len(raw) > MAX_WORKER_REQUEST_BYTES:
            raise ValueError("Decoder request exceeds its metadata bound.")
        entries = json.loads(raw)
        if not isinstance(entries, list) or len(entries) > 20:
            raise ValueError("Upload requires at most 20 files.")
        files = tuple(
            DatasetImportFile(
                name=entry["name"],
                path=Path(entry["path"]),
                size_bytes=entry["size_bytes"],
                sha256=entry["sha256"],
                media_type=entry["media_type"],
            )
            for entry in entries
        )
        result = SpeechDatasetImporter()._import_files(files)
        response = canonical_json(
            SampleSnapshot(
                dataset_revision=result.revision,
                samples=result.samples,
                metadata=result.metadata,
            )
        )
        if len(response) > MAX_WORKER_RESPONSE_BYTES:
            raise ValueError("Upload decoder response exceeds its bound.")
        sys.stdout.buffer.write(response)
        return 0
    except Exception as error:
        from .dataset import DatasetValidationError

        message = (
            str(error)
            if isinstance(error, DatasetValidationError)
            else "Upload decoder failed or exceeded its resource limits."
        )
        sys.stdout.write(json.dumps({"error": message[:500]}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
