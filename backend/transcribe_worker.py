"""Warm Whisper worker used by the Voice Assistant sidecar.

The sidecar keeps one process alive so a transcript request does not reload the
model. Protocol: one JSON object per line on stdin with a ``path`` key; one JSON
object per line on stdout with ``ok`` and either ``text`` or ``error``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from engine.config import load
from engine.stt import Transcriber


def main() -> int:
    config = load()
    transcriber = Transcriber(config)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            path = Path(str(request.get("path") or ""))
            text = transcriber.transcribe(path)
            output = {"ok": True, "text": text}
        except Exception as error:  # noqa: BLE001 - report to the sidecar
            output = {"ok": False, "error": str(error)}
        print(json.dumps(output, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
