"""Run the vendored Voice Assistant API with a parent-liveness guard.

The Orqella sidecar starts this script from the module's virtual environment.
If the sidecar is killed or updated, the child exits instead of leaving an
orphaned API on port 8790.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine.config import load  # noqa: E402
from engine.server import serve  # noqa: E402


def watch_parent(pid: int) -> None:
    while True:
        time.sleep(1.0)
        if pid <= 0:
            continue
        try:
            os.kill(pid, 0)
        except OSError:
            os._exit(0)


def main() -> int:
    parent = int(os.environ.get("VOICE_API_PARENT_PID") or 0)
    if parent > 0:
        threading.Thread(target=watch_parent, args=(parent,), daemon=True).start()
    return serve(load())


if __name__ == "__main__":
    raise SystemExit(main())
