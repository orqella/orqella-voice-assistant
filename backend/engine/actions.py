from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from typing import Any

from .ha import HA, HAError
from .router import Action, Result


class Executor:
    """Run a routed result. ``dry_run`` prints instead of acting."""

    def __init__(self, ha: HA | None = None, dry_run: bool = False):
        self.ha = ha
        self.dry_run = dry_run

    def run(self, result: Result) -> None:
        """Run every action; report the first failure at the end.

        A mode should still dim the lights if one bulb rejects a colour
        temperature, and a missing optional service should not block the rest.
        """
        errors: list[Exception] = []
        for action in result.actions:
            try:
                self._run_one(action)
            except Exception as exc:  # noqa: BLE001 - collected and re-raised
                if not action.ignore_failure:
                    errors.append(exc)
        if errors:
            raise errors[0]

    def _run_one(self, action: Action) -> None:
        if action.service:
            payload: dict[str, Any] = {}
            payload.update(action.target)
            payload.update(action.data)
            if self.dry_run:
                print(f"DRY service {action.service} {payload}")
                return
            if self.ha is None:
                raise HAError("no Home Assistant client configured")
            self.ha.call_service(action.service, payload)
            return

        if action.shell:
            if self.dry_run:
                print(f"DRY shell {action.shell}")
                return
            subprocess.run(
                action.shell,
                check=not action.ignore_failure,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return

        if action.hypr:
            if not sys.platform.startswith("linux") or not shutil.which("hyprctl"):
                raise RuntimeError(
                    "Hyprland actions are only available on a Linux Hyprland desktop"
                )
            argv = ["hyprctl", "dispatch", *shlex.split(action.hypr)]
            if self.dry_run:
                print(f"DRY hypr {argv}")
                return
            subprocess.run(
                argv,
                check=not action.ignore_failure,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return

        if self.dry_run:
            print(f"DRY noop {action}")
