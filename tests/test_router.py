from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from engine.config import Config  # noqa: E402
from engine.router import Router  # noqa: E402


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Config(
            {
                "lights": {"entities": ["light.den", "light.floor"]},
                "media": {"entity": "media_player.den"},
                "apps": {"terminal": ["foot"]},
                "mode": [{"name": "Focus", "aliases": ["deep work"], "say": "Focus mode."}],
            },
            Path("/tmp/router-test.toml"),
        )
        self.router = Router(self.config)

    def test_lights_off_targets_every_configured_entity(self) -> None:
        result = self.router.route("turn off the den lights")
        self.assertEqual(result.kind, "do")
        self.assertEqual(result.actions[0].service, "light.turn_off")
        self.assertEqual(result.actions[0].target["entity_id"], ["light.den", "light.floor"])

    def test_named_color_uses_rgb_or_color_temp(self) -> None:
        result = self.router.route("set the lights to blue")
        self.assertEqual(result.kind, "do")
        self.assertEqual(len(result.actions), 2)
        self.assertEqual(result.actions[0].data["rgb_color"], [0, 64, 255])

    def test_mode_alias_matches(self) -> None:
        result = self.router.route("deep work")
        self.assertEqual(result.kind, "do")
        self.assertIn("Focus", result.text)

    def test_unconfigured_app_is_not_executed(self) -> None:
        result = self.router.route("open definitely-not-configured")
        self.assertEqual(result.kind, "say")
        self.assertIn("don't have an action", result.text)
        self.assertEqual(result.actions, [])

    def test_configured_app_maps_to_fixed_command(self) -> None:
        result = self.router.route("open terminal")
        self.assertEqual(result.actions[0].shell, ["foot"])

    def test_media_mute_uses_the_home_assistant_service(self) -> None:
        result = self.router.route("mute")
        self.assertEqual(result.actions[0].service, "media_player.volume_mute")
        self.assertTrue(result.actions[0].data["is_volume_muted"])

    def test_llm_tools_cannot_inject_shell(self) -> None:
        invalid = self.router.tool_result("open_app", {"app": "terminal; rm -rf /"})
        self.assertIsNone(invalid)
        workspace = self.router.tool_result("workspace", {"number": "1; rm -rf /"})
        self.assertEqual(workspace.actions[0].hypr, "workspace 1")

    def test_opencode_directory_parsing_stays_a_string(self) -> None:
        result = self.router.route("ask opencode in ~/src to fix the tests")
        self.assertEqual(result.kind, "opencode_start")
        self.assertEqual(result.directory, "~/src")
        self.assertEqual(result.text, "fix the tests")
