from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from .config import Config, STATE_DIR


class AudioError(RuntimeError):
    pass


class Recording:
    """A toggle-style recording, usable from separate CLI invocations.

    The PID of ``pw-record`` lives in ``recording.json`` so ``jarvis toggle``
    can start in one process and stop in the next.
    """

    def __init__(self, config: Config):
        self.config = config
        self.state_dir = STATE_DIR
        self.state_file = self.state_dir / "recording.json"
        self.path = self.state_dir / "recording.wav"

    def _state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except Exception:  # noqa: BLE001 - missing/corrupt state is fine
            return {}

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def active(self) -> bool:
        state = self._state()
        try:
            pid = int(state.get("pid", 0))
        except (TypeError, ValueError):
            return False
        return bool(pid and self._alive(pid))

    def start(self, max_seconds: int | None = None) -> Path:
        if self.active():
            raise AudioError("already recording")

        rate = int(self.config.get("audio.rate", 16000))
        source = str(self.config.get("audio.source", "") or "")
        limit = int(max_seconds or self.config.get("audio.max_seconds", 60))

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)

        cmd = [
            "pw-record",
            "--rate",
            str(rate),
            "--channels",
            "1",
            "--format",
            "s16",
            "--container",
            "wav",
            "--sample-count",
            str(rate * limit),
        ]
        if source:
            cmd += ["--target", source]
        cmd.append(str(self.path))

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.15)
        if proc.poll() is not None:
            err = ""
            if proc.stderr:
                err = proc.stderr.read().decode("utf-8", "replace")
            raise AudioError(f"pw-record failed: {err.strip() or 'unknown error'}")

        self.state_file.write_text(
            json.dumps(
                {
                    "pid": proc.pid,
                    "path": str(self.path),
                    "started": time.time(),
                    "max_seconds": limit,
                }
            )
        )
        return self.path

    def stop(self) -> Path | None:
        state = self._state()
        try:
            pid = int(state.get("pid", 0))
        except (TypeError, ValueError):
            pid = 0

        if pid and self._alive(pid):
            try:
                os.kill(pid, signal.SIGINT)
            except OSError:
                pass
            deadline = time.time() + 4
            while time.time() < deadline and self._alive(pid):
                time.sleep(0.05)
            if self._alive(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
                time.sleep(0.1)

        self.state_file.unlink(missing_ok=True)
        path = Path(str(state.get("path") or self.path))
        if path.exists() and path.stat().st_size > 44:
            return path
        return None

    def cancel(self) -> None:
        self.stop()
        for path in (self.path, self.state_file):
            path.unlink(missing_ok=True)
