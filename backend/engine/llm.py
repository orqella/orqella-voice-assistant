from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import Config
from .router import Result

TOOLS: list[dict[str, Any]] = [
    {
        "name": "set_mode",
        "description": "Switch the house to a named mode.",
        "parameters": {
            "type": "object",
            "properties": {"mode": {"type": "string"}},
            "required": ["mode"],
        },
    },
    {
        "name": "lock_desktop",
        "description": "Lock the desktop session.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "system_status",
        "description": "Report GPU, CPU, memory and uptime.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "cat_status",
        "description": "Report the cat's weight and litter box visits.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "feed_cat",
        "description": "Dispense food from the pet feeder.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "clean_litter",
        "description": "Run the litter box cleaning cycle.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "open_app",
        "description": "Open a desktop application.",
        "parameters": {
            "type": "object",
            "properties": {"app": {"type": "string"}},
            "required": ["app"],
        },
    },
    {
        "name": "workspace",
        "description": "Switch to a Hyprland workspace.",
        "parameters": {
            "type": "object",
            "properties": {"number": {"type": "integer"}},
            "required": ["number"],
        },
    },
    {
        "name": "lights",
        "description": (
            "Control the den lights, including the Nanoleaf: on, off, dim, or "
            "set every light to a named color."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["on", "off", "dim", "color"],
                },
                "color": {
                    "type": "string",
                    "description": "red, blue, warm white, cool white, purple, …",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "nanoleaf_effect",
        "description": (
            "Set the Nanoleaf light panels to a named effect such as Nerds, "
            "Ocean, Northern Lights, or Fireworks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "effect": {"type": "string"},
            },
            "required": ["effect"],
        },
    },
    {
        "name": "media",
        "description": "Control playback.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "play_pause",
                        "next",
                        "previous",
                        "volume_up",
                        "volume_down",
                        "mute",
                    ],
                }
            },
            "required": ["action"],
        },
    },
    {
        "name": "samsung_tv",
        "description": "Turn the Samsung TV on or off, or toggle its power.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["on", "off", "toggle"],
                }
            },
            "required": ["action"],
        },
    },
    {
        "name": "apple_tv",
        "description": (
            "Open a show or movie on the Apple TV. Voice Assistant resolves the title "
            "through its local catalog or JustWatch and never invents a URL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Show or movie title, e.g. Community.",
                },
                "service": {
                    "type": "string",
                    "description": (
                        "Optional streaming service, e.g. Peacock, Hulu, "
                        "Disney+, Max, Prime Video, Netflix, Apple TV+."
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": ["open", "play"],
                    "description": "Open the show page or autoplay it.",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "steam_deck",
        "description": (
            "Control the Steam Deck: wake it, put it to sleep, stop the current "
            "game, or launch Big Walk (which wakes the Deck first if needed)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["wake", "play_big_walk", "sleep", "stop"],
                }
            },
            "required": ["action"],
        },
    },
    {
        "name": "opencode_start",
        "description": (
            "Delegate a coding task to a new OpenCode session. Use this when the "
            "user asks to ask, have, tell, or use OpenCode to do something. The "
            "prompt must be complete and self-contained because OpenCode does not "
            "see this conversation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Complete instruction for the OpenCode session.",
                },
                "directory": {
                    "type": "string",
                    "description": (
                        "Project directory (absolute or ~/…). Defaults to the "
                        "configured opencode.default_dir."
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "opencode_status",
        "description": (
            "Check OpenCode jobs started by Voice Assistant. Omit job_id to list recent jobs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
            },
        },
    },
]


class LLM:
    """An optional OpenAI-compatible router (LM Studio, llama.cpp, ollama)."""

    def __init__(self, config: Config):
        self.enabled = bool(config.get("llm.enabled", False))
        self.base_url = str(
            config.get("llm.base_url", "http://127.0.0.1:1234/v1")
        ).rstrip("/")
        self.model = str(config.get("llm.model", "qwen/qwen3.8-27b"))
        self.timeout = float(config.get("llm.timeout", 25))
        self.temperature = float(config.get("llm.temperature", 0.1))
        self.config = config

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode())

    def models(self) -> list[str]:
        try:
            with urllib.request.urlopen(
                f"{self.base_url}/models", timeout=3
            ) as resp:
                data = json.loads(resp.read().decode())
            return [str(item.get("id")) for item in data.get("data", [])]
        except Exception:  # noqa: BLE001
            return []

    def route(self, text: str, router) -> Result | None:
        if not self.enabled:
            return None

        modes = [str(mode.get("name", "")) for mode in self.config.modes]
        apps = list((self.config.get("apps", {}) or {}).keys())
        apple_tv_shows = list(
            (self.config.get("appletv.shows", {}) or {}).keys()
        )
        system = (
            "You are Voice Assistant, a local voice assistant for a Hyprland desktop and "
            "Home Assistant home. Choose exactly one tool when the request is an "
            "action. If the request is a question or small talk, answer briefly "
            "in 'say'. Return JSON only, with keys tool (string or null), args "
            "(object), say (short sentence).\n"
            "If the user asks to ask, have, tell, or use OpenCode to do "
            "something, choose opencode_start with a complete standalone prompt; "
            "do not attempt the coding work yourself. Use opencode_status when "
            "asked how OpenCode is doing.\n"
            "Nanoleaf effects use nanoleaf_effect. Lights colors apply to all "
            "den lights, including the Nanoleaf.\n"
            "Samsung TV power uses samsung_tv (on, off, toggle).\n"
            "Apple TV shows use apple_tv. Voice Assistant can resolve titles through "
            "JustWatch and the local catalog; include service when the user "
            "names one. Do not invent a streaming URL.\n"
            "Steam Deck actions use steam_deck. Big Walk is the only game "
            "currently configured; play_big_walk wakes the Deck first if needed.\n"
            f"Available modes: {', '.join(modes) or 'none'}.\n"
            f"Available apps: {', '.join(apps) or 'none'}.\n"
            f"Apple TV catalog overrides: {', '.join(apple_tv_shows) or 'none'}.\n"
            f"Tools: {json.dumps(TOOLS)}\n"
            # Qwen3 otherwise spends the whole token budget on hidden
            # reasoning and returns an empty content field.
            "/no_think"
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "temperature": self.temperature,
            "max_tokens": 300,
        }
        try:
            data = self._post("/chat/completions", payload)
            content = str(data["choices"][0]["message"].get("content") or "").strip()
            if content.startswith("```"):
                content = content.strip("`")
                if content.lower().startswith("json"):
                    content = content[4:]
                content = content.strip()
            if not content:
                return None
            parsed = json.loads(content)
        except Exception:  # noqa: BLE001 - the LLM is optional
            return None

        tool = parsed.get("tool")
        args = parsed.get("args") or {}
        if tool:
            result = router.tool_result(str(tool), args)
            if result is not None:
                # For OpenCode tools `text` carries the prompt/job id, not a
                # spoken line; the bridge response becomes the spoken result.
                if (
                    parsed.get("say")
                    and not result.kind.startswith("opencode_")
                    and tool != "apple_tv"
                ):
                    result.text = str(parsed["say"])
                return result

        say = parsed.get("say")
        if say:
            return Result("say", str(say))
        return None
