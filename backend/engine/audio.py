from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import Config, STATE_DIR

MODULE_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("VOICE_DATA_ROOT") or (MODULE_ROOT / "data"))
VENV_PYTHON = DATA_DIR / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
RECORDER = MODULE_ROOT / "backend" / "record.py"


class AudioError(RuntimeError):
    pass


def _process_cmdline(pid: int) -> str:
    if not pid:
        return ""
    try:
        if os.path.isdir("/proc"):
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return (result.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _looks_like_recorder(pid: int) -> bool:
    cmdline = _process_cmdline(pid).lower()
    if not cmdline:
        # On platforms without readable command lines, the PID is the best
        # available identity after an explicit start.
        return os.name == "nt"
    return "record.py" in cmdline or "pw-record" in cmdline


class Recording:
    """A toggle-style recording, usable from separate CLI invocations.

    Linux uses PipeWire's ``pw-record``. macOS and Windows use the module's
    managed Python environment with ``sounddevice``. The PID and stop-file
    path live in ``recording.json`` so a later CLI invocation can stop it.
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
        return bool(pid and self._alive(pid) and _looks_like_recorder(pid))

    def start(self, max_seconds: int | None = None) -> Path:
        if self.active():
            raise AudioError("already recording")

        rate = int(self.config.get("audio.rate", 16000))
        source = str(self.config.get("audio.source", "") or "")
        limit = int(max_seconds or self.config.get("audio.max_seconds", 60))

        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.state_dir.chmod(0o700)
        except OSError:
            pass
        self.path.unlink(missing_ok=True)

        stop_file = self.state_dir / "recording.stop"
        stop_file.unlink(missing_ok=True)
        if os.name == "nt" or sys.platform == "darwin":
            python = VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)
            if not RECORDER.is_file():
                raise AudioError("the cross-platform recorder helper is missing")
            cmd = [
                str(python),
                str(RECORDER),
                str(self.path),
                str(rate),
                str(limit),
                str(stop_file),
                source,
            ]
            error_prefix = "sounddevice recorder"
        else:
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
            error_prefix = "pw-record"

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.35 if os.name == "nt" or sys.platform == "darwin" else 0.15)
        if proc.poll() is not None:
            err = ""
            if proc.stderr:
                err = proc.stderr.read().decode("utf-8", "replace")
            raise AudioError(f"{error_prefix} failed: {err.strip() or 'unknown error'}")

        self.state_file.write_text(
            json.dumps(
                {
                    "pid": proc.pid,
                    "path": str(self.path),
                    "stop": str(stop_file),
                    "started": time.time(),
                    "max_seconds": limit,
                }
            )
        )
        try:
            self.state_file.chmod(0o600)
        except OSError:
            pass
        return self.path

    def stop(self) -> Path | None:
        state = self._state()
        try:
            pid = int(state.get("pid", 0))
        except (TypeError, ValueError):
            pid = 0

        cross_platform = os.name == "nt" or sys.platform == "darwin"
        raw_stop = Path(str(state.get("stop") or ""))
        stop_file = raw_stop
        if raw_stop.name:
            try:
                raw_stop.resolve().relative_to(self.state_dir.resolve())
            except ValueError:
                stop_file = None
        else:
            stop_file = None
        if cross_platform and stop_file is not None:
            try:
                stop_file.write_text("stop\n", encoding="utf-8")
            except OSError:
                pass
        if pid and self._alive(pid) and _looks_like_recorder(pid):
            if not cross_platform:
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
                deadline = time.time() + 1
                while time.time() < deadline and self._alive(pid):
                    time.sleep(0.05)
            if self._alive(pid):
                try:
                    os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                except OSError:
                    pass
                time.sleep(0.1)
        if stop_file is not None:
            stop_file.unlink(missing_ok=True)

        self.state_file.unlink(missing_ok=True)
        path = Path(str(state.get("path") or self.path))
        try:
            path.resolve().relative_to(self.state_dir.resolve())
        except ValueError:
            return None
        if path.exists() and path.stat().st_size > 44:
            return path
        return None

    def cancel(self) -> None:
        self.stop()
        for path in (self.path, self.state_file):
            path.unlink(missing_ok=True)
