from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


SUPERVISOR_SCHEMA = "polymarker_quantbot_supervisor_1"
STATUS_SCHEMA = "polymarker_quantbot_supervisor_status_1"
POLL_SECONDS = 10.0
BASE_RESTART_DELAY_SECONDS = 10.0
MAX_RESTART_DELAY_SECONDS = 300.0
STABLE_RUNTIME_SECONDS = 300.0

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
PYTHON_EXE = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
STATE_FILE = DATA_DIR / "supervisor_quantbot_state.json"
STATUS_FILE = DATA_DIR / "supervisor_quantbot_status.json"
LOCK_FILE = DATA_DIR / "supervisor_quantbot.lock"
STOP_FILE = DATA_DIR / "supervisor_quantbot.stop"
SUPERVISOR_LOG = DATA_DIR / "supervisor_quantbot.log"
WORKER_LOG_DIR = DATA_DIR / "supervisor_logs"


@dataclass(frozen=True, slots=True)
class RequiredHash:
    relative_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    name: str
    argv: tuple[str, ...]
    command_markers: tuple[str, ...]
    required_hashes: tuple[RequiredHash, ...]
    completion_files: tuple[str, ...] = ()
    health_kind: str | None = None
    stale_after_seconds: float | None = None
    activation_kind: str = "always"


def default_worker_specs() -> tuple[WorkerSpec, ...]:
    threshold = RequiredHash(
        "data/prereg_v012_thresholds.json",
        "37cc6a00a456e96bc6e3a6abdceccf0fbe2dfd25c0228ed050c978bc14c88883",
    )
    prereg_v013 = RequiredHash(
        "data/prereg_v013_cheap_strict_forward.json",
        "d8c414a5d1562b018fa98d9c408c46da2059555600bc9c1d2065b8eca860ea42",
    )
    monitor_v013 = RequiredHash(
        "v013_monitor.py",
        "4bbeb3f276add707039553ce43a7b9e905d7ff47cc2ed0d2b3a53a455ca2d41b",
    )
    paper_v013 = RequiredHash(
        "paper_trader_v013.py",
        "b71da54e98b32de7f25735c711ef103d053c1ab3388307a17627648bb49715bf",
    )
    prereg_v014 = RequiredHash(
        "data/prereg_v014_disjoint_development.json",
        "c87246dd68861180cd212b1bfe1a2c12fbac1fb26c9ed9c0e3fd26560cad76d0",
    )
    monitor_v014 = RequiredHash(
        "v014_monitor.py",
        "29fc7e23e5b41c59575a70d45b0317346407439c9877e9b35e400585b22ebc16",
    )
    paper_v014 = RequiredHash(
        "paper_trader_v014.py",
        "491ef27d45d6610bdd4f27c03932525807bf58deb225a317d1d0d79641b43a0c",
    )
    calibrator = RequiredHash(
        "calibrar_v012_execution_ev.py",
        "f33cdeef032b45dcfe0850b802bcb8ee45bd31466cb54efc88f022ebbae2b5a9",
    )
    evaluator_v013 = RequiredHash(
        "data/prereg_v013_evaluator.json",
        "242460b2ca1cd3d3d1d00569d4c5b2073628dc6348702f14cb004643d49bd316",
    )
    evaluator_v014 = RequiredHash(
        "data/prereg_v014_evaluator.json",
        "63ead20c541b820168412a4a88fd660f51fc3f10c41983bed91e22df627a6cc6",
    )
    v015_template = RequiredHash(
        "data/prereg_v015_confirmation_template.json",
        "a7d476a36ab8234d7ce87b99c4ecb95f834733e0165ea2fbae8ea2a3ead41210",
    )
    v015_module = RequiredHash(
        "src/polymarket_bot/v015.py",
        "9103904a9523a9fbce3186bff41e28d5a7a0e202c8ccf75f1962cd7fbf06fff6",
    )
    v015_monitor = RequiredHash(
        "v015_monitor.py",
        "508fa87478e3111a4c25e72d00e950d6e0dfb1ed571b8dd924d15c99e7f57f3b",
    )
    v015_paper = RequiredHash(
        "paper_trader_v015.py",
        "f51f27701390a018485a15f78cb0735a5b01c667e7361ce07e4258cfd396154c",
    )
    duration_policy = RequiredHash(
        "data/policy_experiment_duration_24h.json",
        "80855aa19978cce5852dbfb2ac3062c2b3f693daa04bf2f82b7db2eda5f6cd4a",
    )
    runtime_policy = RequiredHash(
        "src/polymarket_bot/runtime_policy.py",
        "ee35120839404e8c48144282409b065adeee37bf8e8f7f26c23123fe132d8083",
    )
    return (
        WorkerSpec(
            name="forward",
            argv=(
                "-m",
                "polymarket_bot",
                "run-shadow",
                "--model-file",
                "data/modelos_twap_transfer_v093.joblib",
                "--output-db",
                "data/shadow_forward_twap_transfer_v094a.db",
                "--hours",
                "168",
                "--max-db-gb",
                "1",
                "--min-free-gb",
                "20",
                "--keep-awake",
            ),
            command_markers=(
                "polymarket_bot",
                "run-shadow",
                "shadow_forward_twap_transfer_v094a.db",
            ),
            required_hashes=(
                RequiredHash(
                    "data/modelos_twap_transfer_v093.joblib",
                    "f39b43d02c916e959297a8a6bbdacbe1cd2323ee0b8243244854339d84b23785",
                ),
                RequiredHash(
                    "src/polymarket_bot/phase41.py",
                    "8835d4c2a03331f539e66ef9ce6648d71c4c4186b9d0205fbe006c2f876707a0",
                ),
                RequiredHash(
                    "src/polymarket_bot/cli.py",
                    "306a39a06f1b2a82b2efb94fcd1ca7ff808ee2bc0b4d484d37a46bb91b23d238",
                ),
                duration_policy,
                runtime_policy,
            ),
            health_kind="forward",
            stale_after_seconds=180.0,
        ),
        WorkerSpec(
            name="execution",
            argv=("execution_collector_v012.py", "--hours", "24"),
            command_markers=("execution_collector_v012.py",),
            required_hashes=(
                RequiredHash(
                    "execution_collector_v012.py",
                    "09ccb6beac6a40fc4c07ad07ef011080f62287604a4130eb747c55a5756f58e1",
                ),
            ),
            completion_files=(
                "data/resultado_v013_forward10.json",
                "data/resultado_v014_development12.json",
            ),
            health_kind="execution",
            stale_after_seconds=900.0,
        ),
        WorkerSpec(
            name="v013_monitor",
            argv=("v013_monitor.py", "--monitor"),
            command_markers=("v013_monitor.py", "--monitor"),
            required_hashes=(
                threshold,
                prereg_v013,
                monitor_v013,
                calibrator,
                evaluator_v013,
            ),
            completion_files=("data/resultado_v013_forward10.json",),
        ),
        WorkerSpec(
            name="v013_paper",
            argv=("paper_trader_v013.py", "--hours", "24"),
            command_markers=("paper_trader_v013.py",),
            required_hashes=(
                threshold,
                prereg_v013,
                monitor_v013,
                paper_v013,
                calibrator,
                evaluator_v013,
            ),
            completion_files=("data/resultado_v013_forward10.json",),
        ),
        WorkerSpec(
            name="v014_monitor",
            argv=("v014_monitor.py", "--monitor"),
            command_markers=("v014_monitor.py", "--monitor"),
            required_hashes=(
                threshold,
                prereg_v013,
                monitor_v013,
                calibrator,
                evaluator_v013,
                prereg_v014,
                monitor_v014,
                paper_v014,
                evaluator_v014,
            ),
            completion_files=("data/resultado_v014_development12.json",),
        ),
        WorkerSpec(
            name="v014_paper",
            argv=("paper_trader_v014.py", "--hours", "24"),
            command_markers=("paper_trader_v014.py",),
            required_hashes=(
                threshold,
                prereg_v013,
                monitor_v013,
                calibrator,
                evaluator_v013,
                prereg_v014,
                monitor_v014,
                paper_v014,
                evaluator_v014,
            ),
            completion_files=("data/resultado_v014_development12.json",),
        ),
        WorkerSpec(
            name="v015_monitor",
            argv=("v015_monitor.py", "--monitor"),
            command_markers=("v015_monitor.py", "--monitor"),
            required_hashes=(
                threshold,
                calibrator,
                prereg_v014,
                evaluator_v014,
                v015_template,
                v015_module,
                v015_monitor,
                v015_paper,
            ),
            completion_files=("data/resultado_v015_confirmation10.json",),
            activation_kind="v015_active",
        ),
        WorkerSpec(
            name="v015_paper",
            argv=("paper_trader_v015.py", "--hours", "24"),
            command_markers=("paper_trader_v015.py",),
            required_hashes=(
                threshold,
                calibrator,
                prereg_v014,
                evaluator_v014,
                v015_template,
                v015_module,
                v015_monitor,
                v015_paper,
            ),
            completion_files=("data/resultado_v015_confirmation10.json",),
            activation_kind="v015_active",
        ),
    )


def default_finalizer_hashes() -> tuple[RequiredHash, ...]:
    return (
        RequiredHash(
            "src/polymarket_bot/finalizer.py",
            "3ce256870fda1d2a55d21c4e65050c73c2d39d19feec4143b45deaa251887f20",
        ),
        RequiredHash(
            "src/polymarket_bot/analysis24.py",
            "cf1a92ab37bf14d17e5b29dc284a42a3e67b7c52c4967bba74d4675cf16d8bfb",
        ),
        RequiredHash(
            "data/prereg_analisis_forward_ultimas24h.json",
            "1e5b7b6bdf107f3e89ec7f4ff9d74b80b957281193d599ac7bb93b92a5b95eaa",
        ),
        RequiredHash(
            "data/policy_experiment_duration_24h.json",
            "80855aa19978cce5852dbfb2ac3062c2b3f693daa04bf2f82b7db2eda5f6cd4a",
        ),
        RequiredHash(
            "src/polymarket_bot/v015.py",
            "9103904a9523a9fbce3186bff41e28d5a7a0e202c8ccf75f1962cd7fbf06fff6",
        ),
        RequiredHash(
            "data/prereg_v015_confirmation_template.json",
            "a7d476a36ab8234d7ce87b99c4ecb95f834733e0165ea2fbae8ea2a3ead41210",
        ),
        RequiredHash(
            "paper_trader_v015.py",
            "f51f27701390a018485a15f78cb0735a5b01c667e7361ce07e4258cfd396154c",
        ),
        RequiredHash(
            "src/polymarket_bot/phase41.py",
            "8835d4c2a03331f539e66ef9ce6648d71c4c4186b9d0205fbe006c2f876707a0",
        ),
        RequiredHash(
            "data/fase4_modelos.db",
            "aefdd422a7188b6a593c939e6b729f31d11a801b2e2960a1cadad885fea3c990",
        ),
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalized_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def sanitized_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if source is None else source)
    sensitive_fragments = (
        "PRIVATE_KEY",
        "SECRET_KEY",
        "SEED_PHRASE",
        "MNEMONIC",
        "WALLET_KEY",
        "API_KEY",
        "API_SECRET",
        "PASSPHRASE",
        "POLYMARKET_PRIVATE",
    )
    for name in tuple(environment):
        upper = name.upper()
        if any(fragment in upper for fragment in sensitive_fragments):
            environment.pop(name, None)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["POLYMARKER_REAL_MONEY"] = "BLOCKED"
    return environment


class ProcessInspector:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    PROCESS_COMMAND_LINE_INFORMATION = 60

    def info(self, pid: int, *, include_command_line: bool = False) -> dict[str, Any] | None:
        if pid <= 0:
            return None
        if os.name != "nt":
            try:
                os.kill(pid, 0)
            except OSError:
                return None
            return {
                "pid": pid,
                "executable": sys.executable,
                "creation_token": None,
                "command_line": None,
            }
        return self._windows_info(pid, include_command_line=include_command_line)

    def _windows_info(
        self,
        pid: int,
        *,
        include_command_line: bool,
    ) -> dict[str, Any] | None:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        handle = open_process(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return None
            if exit_code.value != self.STILL_ACTIVE:
                return None

            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            query_path = kernel32.QueryFullProcessImageNameW
            query_path.argtypes = (
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            )
            query_path.restype = wintypes.BOOL
            if not query_path(handle, 0, buffer, ctypes.byref(size)):
                return None

            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            creation_token = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            command_line = (
                self._windows_command_line(handle) if include_command_line else None
            )
            return {
                "pid": pid,
                "executable": buffer.value,
                "creation_token": str(creation_token),
                "command_line": command_line,
            }
        finally:
            close_handle(handle)

    def _windows_command_line(self, handle: Any) -> str | None:
        from ctypes import wintypes

        class UnicodeString(ctypes.Structure):
            _fields_ = (
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p),
            )

        ntdll = ctypes.WinDLL("ntdll")
        query = ntdll.NtQueryInformationProcess
        query.argtypes = (
            wintypes.HANDLE,
            wintypes.ULONG,
            wintypes.LPVOID,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        )
        query.restype = wintypes.LONG
        needed = wintypes.ULONG()
        query(
            handle,
            self.PROCESS_COMMAND_LINE_INFORMATION,
            None,
            0,
            ctypes.byref(needed),
        )
        if needed.value <= ctypes.sizeof(UnicodeString):
            return None
        raw = ctypes.create_string_buffer(needed.value)
        status = query(
            handle,
            self.PROCESS_COMMAND_LINE_INFORMATION,
            raw,
            needed.value,
            ctypes.byref(needed),
        )
        if status < 0:
            return None
        value = ctypes.cast(raw, ctypes.POINTER(UnicodeString)).contents
        if not value.Buffer or value.Length == 0:
            return ""
        return ctypes.wstring_at(value.Buffer, value.Length // 2)

    def list_by_executable(
        self,
        executable: str | Path,
        *,
        include_command_line: bool = False,
    ) -> list[dict[str, Any]]:
        expected = _normalized_path(executable)
        if os.name != "nt":
            current = self.info(os.getpid(), include_command_line=include_command_line)
            return [current] if current and _normalized_path(current["executable"]) == expected else []

        from ctypes import wintypes

        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        enum_processes = psapi.EnumProcesses
        enum_processes.argtypes = (
            ctypes.POINTER(wintypes.DWORD),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        enum_processes.restype = wintypes.BOOL
        capacity = 4096
        while True:
            process_ids = (wintypes.DWORD * capacity)()
            needed = wintypes.DWORD()
            if not enum_processes(
                process_ids,
                ctypes.sizeof(process_ids),
                ctypes.byref(needed),
            ):
                return []
            count = needed.value // ctypes.sizeof(wintypes.DWORD)
            if count < capacity:
                break
            capacity *= 2

        matches: list[dict[str, Any]] = []
        for pid in process_ids[:count]:
            info = self.info(int(pid), include_command_line=include_command_line)
            if info and _normalized_path(info["executable"]) == expected:
                matches.append(info)
        return sorted(matches, key=lambda item: int(item["pid"]))


class SingleInstance:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "SingleInstance":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("SUPERVISOR_ALREADY_RUNNING") from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class Supervisor:
    def __init__(
        self,
        *,
        root: Path = PROJECT_ROOT,
        python_exe: Path = PYTHON_EXE,
        specs: Iterable[WorkerSpec] | None = None,
        state_file: Path = STATE_FILE,
        status_file: Path = STATUS_FILE,
        worker_log_dir: Path = WORKER_LOG_DIR,
        inspector: ProcessInspector | None = None,
        launcher: Callable[[WorkerSpec], dict[str, Any]] | None = None,
        finalize_callback: Callable[[bool], dict[str, Any]] | None = None,
        finalizer_required_hashes: tuple[RequiredHash, ...] = (),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = root.resolve()
        self.python_exe = python_exe.resolve()
        self.specs = tuple(default_worker_specs() if specs is None else specs)
        self.state_file = state_file
        self.status_file = status_file
        self.worker_log_dir = worker_log_dir
        self.inspector = inspector or ProcessInspector()
        self.clock = clock
        self._launcher = launcher or self._launch_process
        self._finalize_callback = finalize_callback
        self._finalizer_required_hashes = finalizer_required_hashes
        self._hash_cache: dict[tuple[str, int, int], str] = {}

    def load_state(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {
                "schema": SUPERVISOR_SCHEMA,
                "created_at": utc_now(),
                "workers": {},
            }
        payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        if payload.get("schema") != SUPERVISOR_SCHEMA:
            raise ValueError("Schema de estado del supervisor incompatible")
        if not isinstance(payload.get("workers"), dict):
            raise ValueError("Estado del supervisor sin workers validos")
        return payload

    def save_state(self, state: dict[str, Any]) -> None:
        state["updated_at"] = utc_now()
        _atomic_json(self.state_file, state)

    def _digest(self, path: Path) -> str:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = self._hash_cache.get(key)
        if cached is not None:
            return cached
        digest = _sha256(path)
        self._hash_cache[key] = digest
        return digest

    def requirement_mismatches(
        self,
        requirements: Iterable[RequiredHash],
    ) -> list[dict[str, Any]]:
        mismatches: list[dict[str, Any]] = []
        for requirement in requirements:
            path = (self.root / requirement.relative_path).resolve()
            exists = path.is_file()
            actual = self._digest(path) if exists else None
            if actual != requirement.sha256:
                mismatches.append(
                    {
                        "path": requirement.relative_path,
                        "exists": exists,
                        "expected_sha256": requirement.sha256,
                        "actual_sha256": actual,
                    }
                )
        return mismatches

    def hash_mismatches(self, spec: WorkerSpec) -> list[dict[str, Any]]:
        return self.requirement_mismatches(spec.required_hashes)

    def _forward_complete(self) -> bool:
        report = self.root / "auditoria_forward_twap_transfer_v094a_7_dias.txt"
        if report.is_file():
            return True
        database = self.root / "data" / "shadow_forward_twap_transfer_v094a.db"
        if not database.is_file():
            return False
        try:
            uri = f"{database.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=2)
            try:
                row = connection.execute(
                    "SELECT value FROM shadow_meta WHERE key='experiment_completed_at'"
                ).fetchone()
            finally:
                connection.close()
            return bool(row and row[0])
        except (sqlite3.Error, OSError):
            return False

    def is_complete(self, spec: WorkerSpec) -> bool:
        if spec.name == "forward":
            return self._forward_complete()
        if not spec.completion_files:
            return False
        base_complete = all(
            (self.root / relative).is_file() for relative in spec.completion_files
        )
        if not base_complete:
            return False
        if spec.name != "execution":
            return True

        # El collector alimenta tambien la confirmacion v0.15, pero solo cuando
        # el resultado sellado v0.14 selecciona un candidato. Ante un resultado
        # malformado se conserva el collector (fail closed) en vez de detenerlo.
        v014_result = self.root / "data" / "resultado_v014_development12.json"
        v015_active = self.root / "data" / "prereg_v015_confirmation.json"
        v015_result = self.root / "data" / "resultado_v015_confirmation10.json"
        if v015_active.is_file():
            return v015_result.is_file()
        try:
            payload = json.loads(v014_result.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if payload.get("schema") != "resultado_v014_development12":
            return False
        if payload.get("status") == "DEVELOPMENT_CANDIDATE_SELECTED":
            return v015_result.is_file()
        return True

    def _activation_state(
        self,
        spec: WorkerSpec,
        finalizer_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if spec.activation_kind == "always":
            return {"status": "APPLICABLE"}
        if spec.activation_kind != "v015_active":
            return {
                "status": "BLOCKED_DEPENDENCY",
                "error": f"activation_kind desconocido: {spec.activation_kind}",
            }

        if isinstance(finalizer_payload, dict):
            activation = finalizer_payload.get("v015_activation")
            if isinstance(activation, dict):
                status = activation.get("status")
                if status == "ACTIVE":
                    return {"status": "APPLICABLE", "source": "finalizer"}
                if status == "WAITING_V014_RESULT":
                    return {"status": "WAITING_DEPENDENCY", "source": "finalizer"}
                if status == "READY_TO_ACTIVATE":
                    return {"status": "WAITING_ACTIVATION", "source": "finalizer"}
                if status == "NOT_APPLICABLE_V014_NO_CANDIDATE":
                    return {"status": "NOT_APPLICABLE", "source": "finalizer"}
                return {
                    "status": "BLOCKED_DEPENDENCY",
                    "source": "finalizer",
                    "error": f"Estado de activacion v0.15 desconocido: {status}",
                }

        active = self.root / "data" / "prereg_v015_confirmation.json"
        if active.is_file():
            try:
                payload = json.loads(active.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                return {
                    "status": "BLOCKED_DEPENDENCY",
                    "error": f"Prerregistro activo v0.15 invalido: {type(exc).__name__}",
                }
            if (
                payload.get("schema") != "prereg_v015_confirmation10"
                or payload.get("status") != "FROZEN_ACTIVE_CONFIRMATION"
                or payload.get("real_money") != "BLOQUEADO"
            ):
                return {
                    "status": "BLOCKED_DEPENDENCY",
                    "error": "Prerregistro activo v0.15 incompatible",
                }
            return {"status": "APPLICABLE", "source": "active_prereg"}

        result = self.root / "data" / "resultado_v014_development12.json"
        if not result.is_file():
            return {"status": "WAITING_DEPENDENCY", "source": "v014_result"}
        try:
            payload = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "status": "BLOCKED_DEPENDENCY",
                "error": f"Resultado v0.14 invalido: {type(exc).__name__}",
            }
        if payload.get("schema") != "resultado_v014_development12":
            return {
                "status": "BLOCKED_DEPENDENCY",
                "error": "Schema del resultado v0.14 incompatible",
            }
        if payload.get("status") != "DEVELOPMENT_CANDIDATE_SELECTED":
            return {"status": "NOT_APPLICABLE", "source": "v014_result"}
        selected = payload.get("selected_for_v015")
        strata = payload.get("strata_results")
        selected_result = strata.get(selected) if isinstance(strata, dict) else None
        if (
            selected not in ("LOW", "MODERATE")
            or not isinstance(selected_result, dict)
            or selected_result.get("qualifies_for_v015") is not True
        ):
            return {
                "status": "BLOCKED_DEPENDENCY",
                "error": "Seleccion v0.14 para v0.15 inconsistente",
            }
        return {"status": "WAITING_ACTIVATION", "source": "v014_result"}

    def _process_matches(self, entry: dict[str, Any] | None) -> dict[str, Any] | None:
        if not entry or not entry.get("pid"):
            return None
        info = self.inspector.info(int(entry["pid"]))
        if info is None:
            return None
        if _normalized_path(info["executable"]) != _normalized_path(self.python_exe):
            return None
        expected_token = entry.get("creation_token")
        actual_token = info.get("creation_token")
        if expected_token is not None and str(actual_token) != str(expected_token):
            return None
        return info

    def _health(self, spec: WorkerSpec, now_epoch: float) -> dict[str, Any] | None:
        if spec.health_kind == "forward":
            database = self.root / "data" / "shadow_forward_twap_transfer_v094a.db"
            query = "SELECT recorded_at FROM shadow_health ORDER BY recorded_at DESC LIMIT 1"
        elif spec.health_kind == "execution":
            database = self.root / "data" / "execution_forward_v012.db"
            query = "SELECT created_at FROM execution_snapshots ORDER BY created_at DESC LIMIT 1"
        else:
            return None
        if not database.is_file():
            return {"status": "NO_DATABASE", "stale": True}
        try:
            uri = f"{database.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=2)
            try:
                row = connection.execute(query).fetchone()
            finally:
                connection.close()
            if not row or not row[0]:
                return {"status": "NO_HEARTBEAT", "stale": True}
            latest = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
            age = max(0.0, now_epoch - latest.timestamp())
            stale = bool(
                spec.stale_after_seconds is not None
                and age > spec.stale_after_seconds
            )
            return {
                "status": "STALE" if stale else "FRESH",
                "latest_at": str(row[0]),
                "age_seconds": round(age, 3),
                "stale": stale,
            }
        except (sqlite3.Error, OSError, ValueError) as exc:
            return {
                "status": "HEALTH_READ_ERROR",
                "error": f"{type(exc).__name__}: {exc}",
                "stale": True,
            }

    def _launch_process(self, spec: WorkerSpec) -> dict[str, Any]:
        if not self.python_exe.is_file():
            raise RuntimeError(f"No existe Python del proyecto: {self.python_exe}")
        self.worker_log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = self.worker_log_dir / f"{spec.name}.log"
        stderr_path = self.worker_log_dir / f"{spec.name}.err.log"
        command = [str(self.python_exe), *spec.argv]
        creation_flags = 0
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        with stdout_path.open("a", encoding="utf-8", buffering=1) as stdout, stderr_path.open(
            "a", encoding="utf-8", buffering=1
        ) as stderr:
            process = subprocess.Popen(
                command,
                cwd=self.root,
                env=sanitized_environment(),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
                creationflags=creation_flags,
            )
        info = None
        for _ in range(20):
            info = self.inspector.info(process.pid)
            if info is not None:
                break
            time.sleep(0.05)
        if info is None:
            raise RuntimeError(f"No se pudo verificar proceso lanzado PID {process.pid}")
        return info

    def candidates(self) -> list[dict[str, Any]]:
        return self.inspector.list_by_executable(
            self.python_exe,
            include_command_line=True,
        )

    def adopt(self, assignments: dict[str, int]) -> dict[str, Any]:
        state = self.load_state()
        by_name = {spec.name: spec for spec in self.specs}
        unknown_names = sorted(set(assignments) - set(by_name))
        if unknown_names:
            raise ValueError("Workers desconocidos: " + ",".join(unknown_names))
        adopted: dict[str, Any] = {}
        used_pids: set[int] = set()
        for name, pid in assignments.items():
            if pid in used_pids:
                raise ValueError("Un PID no puede asignarse a dos workers")
            used_pids.add(pid)
            info = self.inspector.info(pid, include_command_line=True)
            if info is None:
                raise ValueError(f"PID no accesible o detenido: {pid}")
            if _normalized_path(info["executable"]) != _normalized_path(self.python_exe):
                raise ValueError(f"PID {pid} no usa el Python aprobado")
            command_line = str(info.get("command_line") or "")
            missing = [
                marker
                for marker in by_name[name].command_markers
                if marker.casefold() not in command_line.casefold()
            ]
            if missing:
                raise ValueError(
                    f"PID {pid} no coincide con {name}; faltan marcadores: "
                    + ",".join(missing)
                )
            entry = {
                "pid": pid,
                "creation_token": info.get("creation_token"),
                "adopted": True,
                "last_start_epoch": self.clock(),
                "last_start_at": utc_now(),
                "consecutive_failures": 0,
                "next_start_epoch": 0.0,
                "death_recorded": False,
            }
            state["workers"][name] = entry
            adopted[name] = {
                "pid": pid,
                "command_line": command_line,
            }
        self.save_state(state)
        return {
            "schema": SUPERVISOR_SCHEMA,
            "adopted": adopted,
            "real_money": "BLOQUEADO",
        }

    def _known_live_pids(self, state: dict[str, Any]) -> set[int]:
        known: set[int] = set()
        for entry in state["workers"].values():
            info = self._process_matches(entry)
            if info is not None:
                known.add(int(info["pid"]))
        return known

    def reconcile(self, *, dry_run: bool = False) -> dict[str, Any]:
        state = self.load_state()
        now_epoch = self.clock()
        live_candidates = [
            item
            for item in self.inspector.list_by_executable(self.python_exe)
            if int(item["pid"]) != os.getpid()
        ]
        known_live = self._known_live_pids(state)
        unknown = [
            item for item in live_candidates if int(item["pid"]) not in known_live
        ]
        worker_status: dict[str, Any] = {}

        finalizer_mismatches = self.requirement_mismatches(
            self._finalizer_required_hashes
        )
        finalizer_status: dict[str, Any]
        finalizer_payload: dict[str, Any] | None = None
        if self._finalize_callback is None:
            finalizer_status = {"status": "DISABLED"}
        elif finalizer_mismatches:
            finalizer_status = {
                "status": "BLOCKED_HASH_MISMATCH",
                "hash_mismatches": finalizer_mismatches,
            }
        else:
            try:
                finalizer_payload = self._finalize_callback(not dry_run)
                finalizer_status = {
                    "status": "RECONCILED" if not dry_run else "DRY_RUN_OK",
                    "result": finalizer_payload,
                }
            except Exception as exc:
                finalizer_status = {
                    "status": "ERROR_FAIL_CLOSED",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                logging.exception("El cierre automatico fallo de forma segura")

        for spec in self.specs:
            entry = state["workers"].get(spec.name)
            running = self._process_matches(entry)
            complete = self.is_complete(spec)
            mismatches = self.hash_mismatches(spec)
            item: dict[str, Any] = {
                "completed": complete,
                "hashes_ok": not mismatches,
                "hash_mismatches": mismatches,
                "pid": int(running["pid"]) if running else None,
                "real_money": "BLOQUEADO",
            }

            if complete:
                item["status"] = (
                    "COMPLETED_PROCESS_STILL_RUNNING" if running else "COMPLETED"
                )
                worker_status[spec.name] = item
                continue

            if mismatches:
                item["status"] = (
                    "BLOCKED_HASH_MISMATCH_PROCESS_RUNNING"
                    if running
                    else "BLOCKED_HASH_MISMATCH"
                )
                worker_status[spec.name] = item
                continue

            activation = self._activation_state(spec, finalizer_payload)
            item["activation"] = activation
            if activation["status"] != "APPLICABLE":
                if running:
                    item["status"] = "BLOCKED_DEPENDENCY_PROCESS_RUNNING"
                else:
                    item["status"] = activation["status"]
                if activation["status"] == "NOT_APPLICABLE":
                    item["completed"] = True
                worker_status[spec.name] = item
                continue

            if running:
                entry["last_seen_at"] = utc_now()
                entry["death_recorded"] = False
                health = self._health(spec, now_epoch)
                item["health"] = health
                item["status"] = (
                    "RUNNING_DEGRADED_NO_AUTO_KILL"
                    if health and health.get("stale")
                    else "RUNNING"
                )
                worker_status[spec.name] = item
                continue

            if entry and entry.get("pid") and not entry.get("death_recorded"):
                runtime = max(0.0, now_epoch - float(entry.get("last_start_epoch", now_epoch)))
                failures = int(entry.get("consecutive_failures", 0))
                if runtime >= STABLE_RUNTIME_SECONDS:
                    failures = 0
                failures += 1
                delay = min(
                    MAX_RESTART_DELAY_SECONDS,
                    BASE_RESTART_DELAY_SECONDS * (2 ** max(0, failures - 1)),
                )
                entry["consecutive_failures"] = failures
                entry["next_start_epoch"] = now_epoch + delay
                entry["death_recorded"] = True
                entry["last_exit_detected_at"] = utc_now()

            if unknown:
                item["status"] = "BLOCKED_UNKNOWN_PYTHON_PROCESSES"
                item["unknown_pids"] = [int(value["pid"]) for value in unknown]
                worker_status[spec.name] = item
                continue

            next_start = float(entry.get("next_start_epoch", 0.0)) if entry else 0.0
            if now_epoch < next_start:
                item["status"] = "RESTART_BACKOFF"
                item["restart_in_seconds"] = round(next_start - now_epoch, 3)
                worker_status[spec.name] = item
                continue

            if dry_run:
                item["status"] = "WOULD_START"
                worker_status[spec.name] = item
                continue

            try:
                info = self._launcher(spec)
            except Exception as exc:
                item["status"] = "START_FAILED"
                item["error"] = f"{type(exc).__name__}: {exc}"
                logging.exception("No se pudo iniciar %s", spec.name)
                worker_status[spec.name] = item
                continue

            new_entry = {
                "pid": int(info["pid"]),
                "creation_token": info.get("creation_token"),
                "adopted": False,
                "last_start_epoch": now_epoch,
                "last_start_at": utc_now(),
                "consecutive_failures": int(
                    entry.get("consecutive_failures", 0) if entry else 0
                ),
                "next_start_epoch": 0.0,
                "death_recorded": False,
            }
            state["workers"][spec.name] = new_entry
            known_live.add(int(info["pid"]))
            item["pid"] = int(info["pid"])
            item["status"] = "STARTED"
            worker_status[spec.name] = item
            logging.info("Worker %s iniciado con PID %s", spec.name, info["pid"])

        self.save_state(state)
        payload = {
            "schema": STATUS_SCHEMA,
            "updated_at": utc_now(),
            "supervisor_pid": os.getpid(),
            "python_executable": str(self.python_exe),
            "unknown_python_processes": [
                {
                    "pid": int(item["pid"]),
                    "creation_token": item.get("creation_token"),
                }
                for item in unknown
            ],
            "workers": worker_status,
            "finalizer": finalizer_status,
            "labels_or_outcomes_read": 0,
            "labels_or_outcomes_policy": (
                "El supervisor y el finalizador no leen labels crudos. "
                "La auditoria forward solo se ejecuta tras experiment_completed_at."
            ),
            "orders_enabled": False,
            "wallet_required": False,
            "real_money": "BLOQUEADO",
        }
        _atomic_json(self.status_file, payload)
        return payload


def configure_logging(path: Path = SUPERVISOR_LOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[logging.FileHandler(path, encoding="utf-8")],
        force=True,
    )
    logging.Formatter.converter = time.gmtime


def _parse_adoptions(values: list[str]) -> dict[str, int]:
    assignments: dict[str, int] = {}
    for value in values:
        name, separator, raw_pid = value.partition("=")
        if not separator or not name or not raw_pid:
            raise ValueError("Adopcion debe usar NAME=PID")
        if name in assignments:
            raise ValueError(f"Worker repetido: {name}")
        assignments[name] = int(raw_pid)
    return assignments


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Supervisor fail-closed de los procesos paper-only aprobados"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--monitor", action="store_true")
    group.add_argument("--once", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--candidates", action="store_true")
    group.add_argument("--adopt", action="append", default=[])
    group.add_argument("--stop", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--interval", type=float, default=POLL_SECONDS)
    args = parser.parse_args()

    from polymarket_bot.finalizer import ExperimentFinalizer

    finalizer = ExperimentFinalizer()
    supervisor = Supervisor(
        finalize_callback=lambda mutate: finalizer.reconcile(mutate=mutate),
        finalizer_required_hashes=default_finalizer_hashes(),
    )

    if args.status:
        if not STATUS_FILE.is_file():
            _print_json(
                {
                    "schema": STATUS_SCHEMA,
                    "status": "NO_STATUS_YET",
                    "real_money": "BLOQUEADO",
                }
            )
            return
        _print_json(json.loads(STATUS_FILE.read_text(encoding="utf-8")))
        return

    if args.candidates:
        _print_json(
            {
                "python_executable": str(PYTHON_EXE),
                "candidates": supervisor.candidates(),
                "real_money": "BLOQUEADO",
            }
        )
        return

    if args.stop:
        STOP_FILE.write_text(utc_now(), encoding="utf-8")
        _print_json(
            {
                "stop_requested": True,
                "workers_affected": False,
                "real_money": "BLOQUEADO",
            }
        )
        return

    configure_logging()
    with SingleInstance(LOCK_FILE):
        if args.adopt:
            _print_json(supervisor.adopt(_parse_adoptions(args.adopt)))
            return

        if STOP_FILE.exists():
            STOP_FILE.unlink()

        if args.once:
            _print_json(supervisor.reconcile(dry_run=args.dry_run))
            return

        if args.interval <= 0:
            raise SystemExit("--interval debe ser positivo")
        logging.info("Supervisor iniciado. Dinero real bloqueado.")
        consecutive_reconcile_errors = 0
        while True:
            payload: dict[str, Any] | None = None
            try:
                payload = supervisor.reconcile(dry_run=args.dry_run)
                consecutive_reconcile_errors = 0
            except Exception:
                consecutive_reconcile_errors += 1
                logging.exception(
                    "Fallo transitorio del ciclo %s; supervisor continua fail-closed",
                    consecutive_reconcile_errors,
                )
            if payload is not None and all(
                item["completed"] for item in payload["workers"].values()
            ):
                logging.info("Todos los workflows finalizaron; supervisor termina.")
                return
            deadline = time.time() + args.interval
            while time.time() < deadline:
                if STOP_FILE.exists():
                    STOP_FILE.unlink()
                    logging.info("Supervisor detenido por solicitud; workers continúan.")
                    return
                time.sleep(min(1.0, max(0.0, deadline - time.time())))


__all__ = [
    "ProcessInspector",
    "RequiredHash",
    "SingleInstance",
    "Supervisor",
    "WorkerSpec",
    "default_finalizer_hashes",
    "default_worker_specs",
    "main",
    "sanitized_environment",
]
