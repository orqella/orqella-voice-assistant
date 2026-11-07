from __future__ import annotations

import os
import subprocess
import sys
import time

from .config import Config
from .ha import HA


def _run(argv: list[str]) -> str:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return (proc.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _psutil_status() -> list[str]:
    try:
        import psutil  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return []
    parts: list[str] = []
    try:
        memory = psutil.virtual_memory()
        parts.append(f"{memory.used / (1024**3):.0f} of {memory.total / (1024**3):.0f} GB RAM.")
    except Exception:  # noqa: BLE001
        pass
    try:
        uptime = max(0, int(time.time() - psutil.boot_time()))
        days, remainder = divmod(uptime, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        if days:
            parts.append(f"Up {days}d {hours}h.")
        elif hours:
            parts.append(f"Up {hours}h {minutes}m.")
        else:
            parts.append(f"Up {minutes}m.")
    except Exception:  # noqa: BLE001
        pass
    try:
        temperatures = psutil.sensors_temperatures() or {}
        found_temperature = False
        for entries in temperatures.values():
            for entry in entries:
                if entry.current:
                    parts.append(f"CPU {entry.current:.0f} C.")
                    found_temperature = True
                    break
            if found_temperature:
                break
    except Exception:  # noqa: BLE001
        pass
    return parts


def _linux_status() -> list[str]:
    parts: list[str] = []
    free = _run(["free", "-b"])
    for line in free.splitlines():
        if line.startswith("Mem:"):
            fields = line.split()
            if len(fields) >= 3:
                used = int(fields[2]) / (1024**3)
                total = int(fields[1]) / (1024**3)
                parts.append(f"{used:.0f} of {total:.0f} GB RAM.")
            break
    uptime = _run(["uptime", "-p"])
    if uptime:
        parts.append(f"Up {uptime.replace('up ', '')}.")
    sensors = _run(["sensors"])
    for line in sensors.splitlines():
        if line.strip().startswith("Tctl:"):
            parts.append(f"CPU {line.split(':', 1)[1].strip()}.")
            break
    return parts


def _macos_status() -> list[str]:
    parts: list[str] = []
    total = _run(["sysctl", "-n", "hw.memsize"])
    if total.isdigit():
        parts.append(f"{int(total) / (1024**3):.0f} GB RAM.")
    uptime = _run(["uptime"])
    if uptime:
        parts.append(uptime)
    return parts


def _windows_status() -> list[str]:
    script = (
        "$os = Get-CimInstance Win32_OperatingSystem; "
        "$used = $os.TotalVisibleMemorySize - $os.FreePhysicalMemory; "
        "'{0:N1} of {1:N1} GB RAM. Up {2:N1} hours.' -f "
        "($used/1MB), ($os.TotalVisibleMemorySize/1MB), "
        "((Get-Date) - $os.LastBootUpTime).TotalHours"
    )
    output = _run(["powershell", "-NoProfile", "-Command", script])
    return [output] if output else []


def system_status(config: Config) -> str:
    parts: list[str] = []
    gpu = _run(
        [
            "nvidia-smi",
            "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ]
    )
    if gpu:
        fields = [part.strip() for part in gpu.split(",")]
        if len(fields) >= 4:
            parts.append(
                f"GPU {fields[0]} C, {fields[1]} percent, "
                f"{int(fields[2]) / 1024:.1f} of {int(fields[3]) / 1024:.0f} GB."
            )

    platform_parts = _psutil_status()
    if not platform_parts:
        if sys.platform == "darwin":
            platform_parts = _macos_status()
        elif os.name == "nt":
            platform_parts = _windows_status()
        else:
            platform_parts = _linux_status()
    parts.extend(platform_parts)
    return " ".join(parts) or "I couldn't read the system status."


def cat_status(config: Config, ha: HA) -> str:
    weight_entity = str(config.get("cat.weight", ""))
    visits_entity = str(config.get("cat.visits", ""))
    parts: list[str] = []

    for entity, label in ((weight_entity, "Cat weight"), (visits_entity, "Visits today")):
        if not entity:
            continue
        try:
            state = ha.get_state(entity)
        except Exception:  # noqa: BLE001
            continue
        value = state.get("state")
        unit = (state.get("attributes") or {}).get("unit_of_measurement", "")
        if value not in (None, "unknown", "unavailable"):
            parts.append(f"{label} {value} {unit}".strip())

    if not parts:
        return "I couldn't read the cat sensors."
    return ". ".join(parts) + "."
