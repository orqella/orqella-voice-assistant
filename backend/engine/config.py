from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT / "config.default.toml"
HOST_HOME = Path(os.environ.get("ORQELLA_HOST_HOME") or Path.home()).expanduser()
STATE_HOME = Path(os.environ.get("XDG_STATE_HOME", HOST_HOME / ".local" / "state"))
STATE_DIR = STATE_HOME / "jarvis"


class Config:
    """A tiny dotted-path wrapper around the parsed TOML."""

    def __init__(self, data: dict[str, Any], path: Path):
        self.data = data
        self.path = path

    def get(self, dotted: str, default: Any = None) -> Any:
        current: Any = self.data
        for part in dotted.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        current = self.data
        for part in parts[:-1]:
            nxt = current.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                current[part] = nxt
            current = nxt
        current[parts[-1]] = value

    @property
    def modes(self) -> list[dict[str, Any]]:
        value = self.data.get("mode", [])
        return value if isinstance(value, list) else []

    @property
    def directory(self) -> Path:
        return self.path.parent


def load(path: str | Path | None = None) -> Config:
    raw = str(path or os.environ.get("JARVIS_CONFIG") or DEFAULT_CONFIG)
    if raw == "~":
        chosen = HOST_HOME
    elif raw.startswith("~/"):
        chosen = HOST_HOME / raw[2:]
    else:
        chosen = Path(raw).expanduser()
    if chosen.exists():
        data = tomllib.loads(chosen.read_text())
    else:
        data = {}
    return Config(data, chosen)
