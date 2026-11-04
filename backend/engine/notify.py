from __future__ import annotations

import shutil
import subprocess


def notify(title: str, message: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(
            [
                "notify-send",
                "-a",
                "Voice Assistant",
                "-i",
                "audio-input-microphone",
                title,
                message,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        print(f"{title}: {message}")
