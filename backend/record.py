"""Cross-platform microphone recorder used by the Voice Assistant CLI.

Run this with the module's managed Python environment, which includes
`sounddevice`. The caller writes the stop file to end a hold-to-talk recording.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 5:
        print("usage: record.py PATH RATE MAX_SECONDS STOP_FILE [DEVICE]", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    rate = int(sys.argv[2])
    max_seconds = float(sys.argv[3])
    stop_file = Path(sys.argv[4])
    source = sys.argv[5] if len(sys.argv) > 5 else ""
    watchdog = None
    try:
        # PortAudio can block inside a native call while macOS shows its
        # microphone-permission prompt. A separate process can still end a
        # stuck recorder, unlike a Python timer in the blocked process.
        kill_signal = signal.SIGTERM if os.name == "nt" else signal.SIGKILL
        watchdog_code = (
            f"import os, signal, time; time.sleep({max(30.0, max_seconds + 20.0)}); "
            f"os.kill({os.getpid()}, {int(kill_signal)})"
        )
        watchdog = subprocess.Popen(
            [sys.executable, "-c", watchdog_code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 - the recorder still works without a watchdog
        watchdog = None
    device: str | int | None = None
    if source:
        device = int(source) if source.isdigit() else source

    try:
        import sounddevice as sd
    except Exception as error:  # noqa: BLE001 - surfaced to the CLI
        print(f"sounddevice is unavailable: {error}", file=sys.stderr)
        if watchdog is not None:
            watchdog.terminate()
        return 2

    stop_file.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    ready = threading.Event()
    errors: list[str] = []

    def capture() -> None:
        try:
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(rate)
                with sd.RawInputStream(
                    samplerate=rate,
                    channels=1,
                    dtype="int16",
                    device=device,
                ) as stream:
                    ready.set()
                    deadline = time.time() + max_seconds
                    while not stop_file.exists() and time.time() < deadline:
                        data, _overflowed = stream.read(int(rate * 0.1))
                        output.writeframes(bytes(data))
        except Exception as error:  # noqa: BLE001 - surfaced to the CLI
            errors.append(str(error))
        finally:
            ready.set()

    worker = threading.Thread(target=capture, daemon=True)
    worker.start()
    # A permission prompt or a stuck audio backend must not leave the CLI
    # waiting forever before the stream is even open.
    if not ready.wait(30):
        print(
            "recording failed: timed out waiting for the microphone "
            "(check microphone permission)",
            file=sys.stderr,
        )
        if watchdog is not None:
            watchdog.terminate()
        return 1
    worker.join(max_seconds + 10)
    if worker.is_alive():
        print("recording failed: timed out while recording", file=sys.stderr)
        stop_file.unlink(missing_ok=True)
        if watchdog is not None:
            watchdog.terminate()
        return 1
    stop_file.unlink(missing_ok=True)
    if watchdog is not None:
        watchdog.terminate()
    if errors:
        print(f"recording failed: {errors[-1]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
