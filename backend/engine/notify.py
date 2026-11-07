from __future__ import annotations

import os
import shutil
import subprocess
import sys


def _escape_apple(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _escape_powershell(value: str) -> str:
    return value.replace("'", "''")


def notify(title: str, message: str) -> None:
    if sys.platform == "darwin" and shutil.which("osascript"):
        script = (
            f'display notification "{_escape_apple(message)}" '
            f'with title "{_escape_apple(title)}"'
        )
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    if os.name == "nt" and shutil.which("powershell"):
        title_ps = _escape_powershell(title)
        message_ps = _escape_powershell(message)
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -AssemblyName System.Drawing; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Information; "
            f"$n.BalloonTipTitle = '{title_ps}'; "
            f"$n.BalloonTipText = '{message_ps}'; "
            "$n.Visible = $true; $n.ShowBalloonTip(5000); "
            "Start-Sleep -Milliseconds 800; $n.Dispose()"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
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
        return
    print(f"{title}: {message}")
