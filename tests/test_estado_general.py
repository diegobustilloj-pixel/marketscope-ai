import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import estado_general


class EstadoGeneralTests(unittest.TestCase):
    def test_stale_supervisor_status_is_not_reported_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            payload = {
                "updated_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=5)
                ).isoformat(),
                "unknown_python_processes": [],
                "workers": {"forward": {"status": "RUNNING"}},
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(estado_general, "SUPERVISOR_STATUS", path):
                status = estado_general._supervisor_state()

        self.assertFalse(status["fresh"])
        self.assertFalse(status["healthy"])

    def test_recent_valid_supervisor_status_is_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            payload = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "unknown_python_processes": [],
                "workers": {
                    "forward": {"status": "RUNNING"},
                    "v015": {"status": "NOT_APPLICABLE"},
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(estado_general, "SUPERVISOR_STATUS", path):
                status = estado_general._supervisor_state()

        self.assertTrue(status["fresh"])
        self.assertTrue(status["healthy"])


if __name__ == "__main__":
    unittest.main()
