from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
SCRIPT = """
import json
import sys
sys.path.insert(0, %r)
import catalog

data = catalog.voice_capabilities()
print(json.dumps({
    "groups": data["groups"],
    "phrase_count": data["phrase_count"],
}))
""" % str(BACKEND)


class CatalogTests(unittest.TestCase):
    def run_catalog(self, config_text: str) -> dict:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp) / "data"
            data_dir.mkdir()
            (data_dir / "config.toml").write_text(config_text, encoding="utf-8")
            env = os.environ.copy()
            env["VOICE_DATA_ROOT"] = str(data_dir)
            result = subprocess.run(
                [sys.executable, "-c", SCRIPT],
                cwd=temp,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_default_config_has_no_routes(self) -> None:
        config = (BACKEND / "config.default.toml").read_text(encoding="utf-8")
        data = self.run_catalog(config)
        self.assertEqual(data["groups"], [])
        self.assertEqual(data["phrase_count"], 0)

    def test_empty_app_commands_do_not_create_routes(self) -> None:
        config = """
[input]
builtin_routes = false

[apps]
terminal = []
files = []
"""
        data = self.run_catalog(config)
        self.assertEqual(data["groups"], [])

    def test_configured_app_creates_only_its_route(self) -> None:
        config = """
[input]
builtin_routes = false

[apps]
terminal = ["foot"]
"""
        data = self.run_catalog(config)
        self.assertEqual([group["id"] for group in data["groups"]], ["apps"])
        self.assertEqual([item["name"] for item in data["groups"][0]["items"]], ["Terminal"])


if __name__ == "__main__":
    unittest.main()
