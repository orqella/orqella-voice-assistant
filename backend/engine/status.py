from __future__ import annotations

import subprocess
from typing import Any

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

    sensors = _run(["sensors"])
    for line in sensors.splitlines():
        if line.strip().startswith("Tctl:"):
            temp = line.split(":", 1)[1].strip()
            parts.append(f"CPU {temp}.")
            break

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
