from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import Config, STATE_DIR


class ModelError(RuntimeError):
    pass


def lms_binary() -> str:
    found = shutil.which("lms")
    if found:
        return found
    candidate = Path.home() / ".lmstudio" / "bin" / "lms"
    if candidate.is_file():
        return str(candidate)
    raise ModelError(
        "lms not found; install LM Studio or add ~/.lmstudio/bin to PATH"
    )


def _run(argv: list[str], timeout: int) -> str:
    cmd = [lms_binary(), *argv]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ModelError(
            f"lms {' '.join(argv)} timed out after {timeout}s"
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ModelError(detail or f"lms {' '.join(argv)} exited {proc.returncode}")
    return (proc.stdout or "").strip()


def loaded_models() -> list[dict[str, Any]]:
    try:
        raw = _run(["ps", "--json"], timeout=10)
    except ModelError:
        return []
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def find_loaded(
    model: str, models: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """Return the loaded entry for ``model`` (identifier or model key)."""
    entries = loaded_models() if models is None else models
    for entry in entries:
        names = {
            str(entry.get("identifier") or ""),
            str(entry.get("modelKey") or ""),
        }
        if model in names:
            return entry
    return None


def ensure_server() -> None:
    try:
        _run(["server", "start"], timeout=30)
    except ModelError:
        # "start" can be noisy if it is already running; status is the truth.
        _run(["server", "status"], timeout=5)


def load(model: str, config: Config) -> str:
    ensure_server()
    args = list(
        config.get(
            "llm.load_args",
            ["--gpu", "max", "--context-length", "8192"],
        )
        or []
    )
    argv = ["load", model, *args]
    if "-y" not in argv and "--yes" not in argv:
        argv.append("-y")
    return _run(argv, timeout=int(config.get("llm.load_timeout", 180)))


def unload(model: str) -> str:
    return _run(["unload", model], timeout=30)


def marker_path() -> Path:
    """Sentinel read by hyprdeck's ``icon_when`` rule."""
    return STATE_DIR / "model-loaded"


def sync_marker(loaded: bool) -> bool:
    """Create or remove the model-loaded marker.

    hyprdeck polls this path for the B20 icon, so changing it is enough; the
    deck does not need a restart. Returns True when the marker changed.
    """
    path = marker_path()
    if loaded:
        if path.is_file() and path.read_text().strip() == "1":
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1\n")
        return True
    if path.is_file():
        path.unlink()
        return True
    return False
