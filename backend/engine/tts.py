from __future__ import annotations

import subprocess

from .config import Config
from .ha import HA
from .notify import notify


class Speaker:
    """Notifications first, optional local TTS, optional HA announce."""

    def __init__(self, config: Config, ha: HA | None = None):
        self.enabled = bool(config.get("tts.enabled", False))
        self.command = list(config.get("tts.command", []) or [])
        self.output_file = str(config.get("tts.output_file", "") or "")
        self.text_stdin = bool(config.get("tts.text_stdin", True))
        self.notify_service = str(config.get("tts.notify_service", "") or "")
        self.ha = ha

    def say(self, text: str) -> None:
        if not text:
            return
        print(text)
        notify("Voice Assistant", text)
        if not self.enabled:
            return

        if self.command:
            try:
                argv = list(self.command)
                if self.text_stdin:
                    subprocess.run(argv, input=text, text=True, check=True)
                else:
                    subprocess.run(argv + [text], check=True)
                if self.output_file:
                    subprocess.run(
                        ["pw-play", self.output_file],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                return
            except Exception:  # noqa: BLE001 - fall through to HA
                pass

        if self.notify_service and self.ha is not None:
            try:
                self.ha.call_service(self.notify_service, {"message": text})
            except Exception:  # noqa: BLE001
                pass
