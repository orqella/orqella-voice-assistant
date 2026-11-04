from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"


class VoicePreflightTests(unittest.TestCase):
    def test_engine_api_module_imports(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, %r); import engine.server; print(engine.server.DEFAULT_PORT)" % str(BACKEND)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("8790", result.stdout)

    def test_old_python_starts_and_reports_a_clear_error(self) -> None:
        script = """
import sys
sys.version_info = (3, 9, 0)
import server

state = server.setup_status()
assert state["python_ok"] is False, state
assert "3.11" in state["error"], state
assert state["ready"] is False, state
try:
    server.catalog_module()
except RuntimeError as error:
    assert "3.11" in str(error), error
else:
    raise AssertionError("catalog imported on an unsupported Python")
server.run_setup()
state = server.setup_status()
assert state["job"] == "failed", state
assert "3.11" in state["log"], state
print("ok")
"""
        with tempfile.TemporaryDirectory() as temp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(BACKEND)
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=temp,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
