import hashlib
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from polymarket_bot.supervisor import (
    RequiredHash,
    Supervisor,
    WorkerSpec,
    default_worker_specs,
    sanitized_environment,
)


class FakeClock:
    def __init__(self, value=1_000.0):
        self.value = float(value)

    def __call__(self):
        return self.value


class FakeInspector:
    def __init__(self, executable):
        self.executable = str(executable)
        self.processes = {}

    def add(self, pid, token, command_line=""):
        self.processes[int(pid)] = {
            "pid": int(pid),
            "executable": self.executable,
            "creation_token": str(token),
            "command_line": command_line,
        }

    def remove(self, pid):
        self.processes.pop(int(pid), None)

    def info(self, pid, *, include_command_line=False):
        item = self.processes.get(int(pid))
        if item is None:
            return None
        result = dict(item)
        if not include_command_line:
            result["command_line"] = None
        return result

    def list_by_executable(self, executable, *, include_command_line=False):
        return [
            self.info(pid, include_command_line=include_command_line)
            for pid in sorted(self.processes)
        ]


class SupervisorHarness:
    def __init__(self, root):
        self.root = Path(root)
        self.python = self.root / ".venv" / "Scripts" / "python.exe"
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b"python")
        contract = self.root / "contract.txt"
        contract.write_text("frozen", encoding="utf-8")
        digest = hashlib.sha256(contract.read_bytes()).hexdigest()
        self.spec = WorkerSpec(
            name="worker",
            argv=("worker.py", "--monitor"),
            command_markers=("worker.py", "--monitor"),
            required_hashes=(RequiredHash("contract.txt", digest),),
            completion_files=("done.json",),
        )
        self.inspector = FakeInspector(self.python)
        self.clock = FakeClock()
        self.launches = []
        self.next_pid = 100

        def launcher(spec):
            self.next_pid += 1
            self.launches.append(spec.name)
            command_line = f'"{self.python}" worker.py --monitor'
            self.inspector.add(self.next_pid, f"token-{self.next_pid}", command_line)
            return self.inspector.info(self.next_pid)

        self.supervisor = Supervisor(
            root=self.root,
            python_exe=self.python,
            specs=(self.spec,),
            state_file=self.root / "state.json",
            status_file=self.root / "status.json",
            worker_log_dir=self.root / "logs",
            inspector=self.inspector,
            launcher=launcher,
            clock=self.clock,
        )


class SupervisorTests(unittest.TestCase):
    def test_starts_missing_worker_once_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = SupervisorHarness(directory)

            first = harness.supervisor.reconcile()
            second = harness.supervisor.reconcile()

        self.assertEqual(first["workers"]["worker"]["status"], "STARTED")
        self.assertEqual(second["workers"]["worker"]["status"], "RUNNING")
        self.assertEqual(harness.launches, ["worker"])
        self.assertEqual(first["labels_or_outcomes_read"], 0)
        self.assertFalse(first["orders_enabled"])
        self.assertFalse(first["wallet_required"])

    def test_unknown_project_python_blocks_start(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = SupervisorHarness(directory)
            harness.inspector.add(900, "unknown", "python unknown.py")

            result = harness.supervisor.reconcile()

        worker = result["workers"]["worker"]
        self.assertEqual(worker["status"], "BLOCKED_UNKNOWN_PYTHON_PROCESSES")
        self.assertEqual(worker["unknown_pids"], [900])
        self.assertEqual(harness.launches, [])

    def test_supervisor_console_pid_does_not_block_its_own_reconcile(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = SupervisorHarness(directory)
            harness.inspector.add(os.getpid(), "self", "python supervisor_quantbot.py --once")

            result = harness.supervisor.reconcile()

        self.assertEqual(result["workers"]["worker"]["status"], "STARTED")
        self.assertEqual(harness.launches, ["worker"])

    def test_hash_mismatch_blocks_start(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = SupervisorHarness(directory)
            (Path(directory) / "contract.txt").write_text("changed", encoding="utf-8")

            result = harness.supervisor.reconcile()

        worker = result["workers"]["worker"]
        self.assertEqual(worker["status"], "BLOCKED_HASH_MISMATCH")
        self.assertFalse(worker["hashes_ok"])
        self.assertEqual(harness.launches, [])

    def test_completion_marker_prevents_start(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = SupervisorHarness(directory)
            (Path(directory) / "done.json").write_text("{}", encoding="utf-8")

            result = harness.supervisor.reconcile()

        self.assertEqual(result["workers"]["worker"]["status"], "COMPLETED")
        self.assertEqual(harness.launches, [])

    def test_dead_worker_uses_backoff_then_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = SupervisorHarness(directory)
            first = harness.supervisor.reconcile()
            first_pid = first["workers"]["worker"]["pid"]
            harness.inspector.remove(first_pid)
            harness.clock.value += 5.0

            backoff = harness.supervisor.reconcile()
            harness.clock.value += 11.0
            restarted = harness.supervisor.reconcile()

        self.assertEqual(backoff["workers"]["worker"]["status"], "RESTART_BACKOFF")
        self.assertEqual(restarted["workers"]["worker"]["status"], "STARTED")
        self.assertEqual(harness.launches, ["worker", "worker"])

    def test_adoption_requires_exact_command_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = SupervisorHarness(directory)
            harness.inspector.add(500, "token", "python wrong.py")
            with self.assertRaises(ValueError):
                harness.supervisor.adopt({"worker": 500})

            harness.inspector.processes[500]["command_line"] = (
                "python worker.py --monitor"
            )
            adopted = harness.supervisor.adopt({"worker": 500})
            result = harness.supervisor.reconcile()

        self.assertEqual(adopted["adopted"]["worker"]["pid"], 500)
        self.assertEqual(result["workers"]["worker"]["status"], "RUNNING")
        self.assertEqual(harness.launches, [])

    def test_pid_reuse_is_treated_as_unknown_not_as_the_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = SupervisorHarness(directory)
            harness.inspector.add(500, "original", "python worker.py --monitor")
            harness.supervisor.adopt({"worker": 500})
            harness.inspector.processes[500]["creation_token"] = "reused"

            result = harness.supervisor.reconcile()

        worker = result["workers"]["worker"]
        self.assertEqual(worker["status"], "BLOCKED_UNKNOWN_PYTHON_PROCESSES")
        self.assertEqual(worker["unknown_pids"], [500])

    def test_forward_completion_uses_only_safe_meta(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = SupervisorHarness(directory)
            database = Path(directory) / "data" / "shadow_forward_twap_transfer_v094a.db"
            database.parent.mkdir()
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE shadow_meta(key TEXT PRIMARY KEY,value TEXT)"
            )
            connection.execute(
                "INSERT INTO shadow_meta(key,value) VALUES(?,?)",
                ("experiment_completed_at", "2026-08-17T00:02:18+00:00"),
            )
            connection.commit()
            connection.close()
            forward = WorkerSpec(
                name="forward",
                argv=("-m", "polymarket_bot", "run-shadow"),
                command_markers=("run-shadow",),
                required_hashes=(),
            )
            supervisor = Supervisor(
                root=Path(directory),
                python_exe=harness.python,
                specs=(forward,),
                state_file=Path(directory) / "forward-state.json",
                status_file=Path(directory) / "forward-status.json",
                inspector=harness.inspector,
                launcher=lambda spec: self.fail("No debe lanzar forward completo"),
            )

            result = supervisor.reconcile()

        self.assertEqual(result["workers"]["forward"]["status"], "COMPLETED")

    def test_sensitive_environment_is_removed(self):
        environment = sanitized_environment(
            {
                "PATH": "safe",
                "PRIVATE_KEY": "no",
                "wallet_key_prod": "no",
                "POLYMARKET_API_KEY": "no",
                "API_SECRET": "no",
                "PASSPHRASE": "no",
                "PUBLIC_URL": "yes",
            }
        )
        self.assertEqual(environment["PATH"], "safe")
        self.assertEqual(environment["PUBLIC_URL"], "yes")
        self.assertNotIn("PRIVATE_KEY", environment)
        self.assertNotIn("wallet_key_prod", environment)
        self.assertNotIn("POLYMARKET_API_KEY", environment)
        self.assertNotIn("API_SECRET", environment)
        self.assertNotIn("PASSPHRASE", environment)
        self.assertEqual(environment["POLYMARKER_REAL_MONEY"], "BLOCKED")

    def test_default_contract_has_six_base_and_two_conditional_workers(self):
        specs = default_worker_specs()
        self.assertEqual(
            [spec.name for spec in specs],
            [
                "forward",
                "execution",
                "v013_monitor",
                "v013_paper",
                "v014_monitor",
                "v014_paper",
                "v015_monitor",
                "v015_paper",
            ],
        )
        self.assertTrue(
            all(spec.activation_kind == "v015_active" for spec in specs[-2:])
        )
        joined = " ".join(part for spec in specs for part in spec.argv).lower()
        self.assertNotIn("private-key", joined)
        self.assertNotIn("wallet", joined)
        self.assertNotIn("live-order", joined)

    def test_v015_waits_without_v014_result(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = SupervisorHarness(directory)
            spec = replace(
                harness.spec,
                name="v015_monitor",
                activation_kind="v015_active",
            )
            harness.supervisor.specs = (spec,)

            result = harness.supervisor.reconcile()

        self.assertEqual(
            result["workers"]["v015_monitor"]["status"],
            "WAITING_DEPENDENCY",
        )
        self.assertEqual(harness.launches, [])

    def test_v015_starts_only_with_compatible_active_preregistration(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = SupervisorHarness(directory)
            spec = replace(
                harness.spec,
                name="v015_monitor",
                activation_kind="v015_active",
            )
            harness.supervisor.specs = (spec,)
            data = Path(directory) / "data"
            data.mkdir()
            (data / "prereg_v015_confirmation.json").write_text(
                '{"schema":"prereg_v015_confirmation10",'
                '"status":"FROZEN_ACTIVE_CONFIRMATION",'
                '"real_money":"BLOQUEADO"}',
                encoding="utf-8",
            )

            result = harness.supervisor.reconcile()

        self.assertEqual(result["workers"]["v015_monitor"]["status"], "STARTED")
        self.assertEqual(harness.launches, ["v015_monitor"])

    def test_finalizer_callback_is_hash_guarded(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = SupervisorHarness(directory)
            contract = Path(directory) / "contract.txt"
            digest = hashlib.sha256(contract.read_bytes()).hexdigest()
            calls = []
            harness.supervisor._finalize_callback = lambda mutate: (
                calls.append(mutate) or {"v015_activation": {"status": "WAITING_V014_RESULT"}}
            )
            harness.supervisor._finalizer_required_hashes = (
                RequiredHash("contract.txt", digest),
            )

            good = harness.supervisor.reconcile(dry_run=True)
            contract.write_text("tampered", encoding="utf-8")
            bad = harness.supervisor.reconcile(dry_run=True)

        self.assertEqual(calls, [False])
        self.assertEqual(good["finalizer"]["status"], "DRY_RUN_OK")
        self.assertEqual(bad["finalizer"]["status"], "BLOCKED_HASH_MISMATCH")

    def test_execution_stays_alive_only_when_v015_is_applicable(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = SupervisorHarness(directory)
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            (data / "resultado_v013_forward10.json").write_text("{}", encoding="utf-8")
            v014 = data / "resultado_v014_development12.json"
            v014.write_text(
                '{"schema":"resultado_v014_development12",'
                '"status":"FAIL_NO_DEVELOPMENT_CANDIDATE"}',
                encoding="utf-8",
            )
            execution = WorkerSpec(
                name="execution",
                argv=("collector.py",),
                command_markers=("collector.py",),
                required_hashes=(),
                completion_files=(
                    "data/resultado_v013_forward10.json",
                    "data/resultado_v014_development12.json",
                ),
            )

            self.assertTrue(harness.supervisor.is_complete(execution))
            v014.write_text(
                '{"schema":"resultado_v014_development12",'
                '"status":"DEVELOPMENT_CANDIDATE_SELECTED"}',
                encoding="utf-8",
            )
            self.assertFalse(harness.supervisor.is_complete(execution))
            (data / "resultado_v015_confirmation10.json").write_text(
                "{}", encoding="utf-8"
            )
            self.assertTrue(harness.supervisor.is_complete(execution))


if __name__ == "__main__":
    unittest.main()
