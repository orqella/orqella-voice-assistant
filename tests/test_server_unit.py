"""Unit tests for the Voice Assistant sidecar's config and route helpers."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# The sidecar treats the current directory as the installed module root and
# creates its data directory on import, so isolate it in a temp directory.
_ROOT = tempfile.TemporaryDirectory()
os.chdir(_ROOT.name)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import server  # noqa: E402


class VoiceServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_data = server.DATA
        self.old_config_file = server.CONFIG_FILE
        self.old_state_file = server.STATE_FILE
        server.DATA = self.root / "data"
        server.DATA.mkdir()
        server.CONFIG_FILE = server.DATA / "config.toml"
        server.STATE_FILE = server.DATA / "route_state.json"
        server.CONFIG_FILE.write_text(
            """
[[mode]]
name = "Focus"
prompt = "Focus mode"

[[mode]]
name = "Relax"
prompt = "Relax mode"

[apps]
youtube = { query = "https://youtube.com" }
music = { query = "https://music.example" }

[appletv.shows.severance]
title = "Severance"
""".lstrip(),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        server.DATA = self.old_data
        server.CONFIG_FILE = self.old_config_file
        server.STATE_FILE = self.old_state_file
        self.temp.cleanup()

    def test_display_text_and_route_keys_are_stable(self) -> None:
        self.assertEqual(
            server.display_text("Jarvis's jarvis and Jarvis"),
            "voice engine's voice engine and voice engine",
        )
        self.assertEqual(server.route_key("Modes", "Focus Mode!"), "Modes/focus-mode")
        self.assertEqual(server.route_key("Modes", "  "), "Modes/")
        self.assertEqual(server.normal("What's the weather?"), "what's the weather")
        self.assertEqual(server.normal(None), "")

    def test_toml_string_handles_quotes_and_plain_values(self) -> None:
        self.assertEqual(server._toml_string('"value"'), "value")
        self.assertEqual(server._toml_string("'value'"), "value")
        self.assertEqual(server._toml_string("plain"), "plain")
        self.assertEqual(server._toml_string(None), "")

    def test_disabled_routes_round_trip_and_tolerate_corruption(self) -> None:
        self.assertEqual(server.load_disabled(), set())
        server.save_disabled({"modes/focus", "apps/youtube"})
        self.assertEqual(server.load_disabled(), {"modes/focus", "apps/youtube"})
        server.STATE_FILE.write_text("{not json", encoding="utf-8")
        self.assertEqual(server.load_disabled(), set())
        server.STATE_FILE.write_text(json.dumps({"disabled": [1, "two"]}), encoding="utf-8")
        self.assertEqual(server.load_disabled(), {"1", "two"})

    def test_delete_config_route_removes_modes_apps_and_shows(self) -> None:
        fake_catalog = mock.Mock()
        with mock.patch.object(server, "catalog_module", return_value=fake_catalog):
            self.assertTrue(
                server.delete_config_route({"type": "mode", "name": "focus"})["ok"]
            )
            text = server.CONFIG_FILE.read_text(encoding="utf-8")
            self.assertNotIn('name = "Focus"', text)
            self.assertIn('name = "Relax"', text)

            server.delete_config_route({"type": "app", "key": "youtube"})
            text = server.CONFIG_FILE.read_text(encoding="utf-8")
            self.assertNotIn("youtube", text)
            self.assertIn("music", text)

            server.delete_config_route({"type": "appletv_show", "key": "severance"})
            text = server.CONFIG_FILE.read_text(encoding="utf-8")
            self.assertNotIn("severance", text)
        self.assertEqual(fake_catalog.invalidate_cache.call_count, 3)
        backups = list(server.DATA.glob("config.toml.bak"))
        self.assertEqual(len(backups), 1)

    def test_delete_config_route_rejects_unknown_targets(self) -> None:
        with mock.patch.object(server, "catalog_module", return_value=mock.Mock()):
            with self.assertRaises(ValueError):
                server.delete_config_route({"type": "mode", "name": "Missing"})
            with self.assertRaises(ValueError):
                server.delete_config_route({"type": "app", "key": "missing"})
            with self.assertRaises(ValueError):
                server.delete_config_route({"type": "appletv_show", "key": "missing"})
            with self.assertRaises(ValueError):
                server.delete_config_route({"type": "engine"})
            with self.assertRaises(ValueError):
                server.delete_config_route(None)

    def test_catalog_rewrites_names_keys_and_disabled_state(self) -> None:
        server.save_disabled({"modes/focus-mode"})
        data = {
            "groups": [
                {
                    "id": "modes",
                    "label": "Jarvis modes",
                    "description": "Ask Jarvis",
                    "source": "Jarvis config",
                    "items": [
                        {
                            "name": "Focus Mode",
                            "description": "Jarvis focus",
                            "detail": "jarvis detail",
                            "phrases": ["focus mode"],
                        }
                    ],
                }
            ]
        }
        fake = mock.Mock()
        fake.voice_capabilities.return_value = data
        with mock.patch.object(server, "catalog_module", return_value=fake):
            result = server.catalog(True)
        group = result["groups"][0]
        self.assertEqual(group["label"], "voice engine modes")
        self.assertEqual(group["description"], "Ask voice engine")
        self.assertEqual(group["source"], "voice engine config")
        item = group["items"][0]
        self.assertEqual(item["name"], "Focus Mode")
        self.assertEqual(item["description"], "voice engine focus")
        self.assertEqual(item["detail"], "voice engine detail")
        self.assertEqual(item["key"], "modes/focus-mode")
        self.assertFalse(item["enabled"])
        fake.voice_capabilities.assert_called_once_with(True)

    def test_match_route_picks_the_longest_phrase(self) -> None:
        data = {
            "groups": [
                {
                    "id": "apps",
                    "label": "Apps",
                    "items": [
                        {
                            "name": "Music",
                            "key": "apps/music",
                            "phrases": ["play music"],
                            "enabled": True,
                        },
                        {
                            "name": "Focus",
                            "key": "apps/focus",
                            "phrases": ["play focus music"],
                            "enabled": False,
                        },
                    ],
                }
            ]
        }
        server.save_disabled({"apps/focus"})
        fake = mock.Mock()
        fake.voice_capabilities.return_value = data
        with mock.patch.object(server, "catalog_module", return_value=fake):
            result = server.match_route("Please play focus music now")
            self.assertEqual(result["name"], "Focus")
            self.assertEqual(result["key"], "apps/focus")
            self.assertFalse(result["enabled"])
            self.assertIsNone(server.match_route(""))
            self.assertIsNone(server.match_route("unrelated request"))

    def test_setup_status_and_api_settings(self) -> None:
        state = server.setup_status()
        self.assertEqual(state["python_ok"], server.PYTHON_SUPPORTED)
        self.assertFalse(state["ready"])
        self.assertIn("python_required", state)
        self.assertEqual(state["data_root"], str(server.DATA))

        server.CONFIG_FILE.write_text(
            """
[api]
host = "0.0.0.0"
port = 9999
token = "secret"
""".lstrip(),
            encoding="utf-8",
        )
        self.assertEqual(server.api_settings(), ("0.0.0.0", 9999, "secret"))
        server.CONFIG_FILE.write_text("not = [valid", encoding="utf-8")
        self.assertEqual(server.api_settings(), ("127.0.0.1", 8790, ""))

    def test_write_config_is_atomic_and_keeps_a_backup(self) -> None:
        original = server.CONFIG_FILE.read_text(encoding="utf-8")
        server._write_config(original + "\n# updated\n")
        self.assertIn("# updated", server.CONFIG_FILE.read_text(encoding="utf-8"))
        backup = server.CONFIG_FILE.with_name(server.CONFIG_FILE.name + ".bak")
        self.assertEqual(backup.read_text(encoding="utf-8"), original)
        self.assertEqual(list(server.DATA.glob("config-*.toml")), [])


if __name__ == "__main__":
    unittest.main()
