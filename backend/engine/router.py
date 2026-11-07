from __future__ import annotations

import colorsys
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import Config
from .deeplinks import DeepLink


@dataclass
class Action:
    service: str | None = None
    target: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    shell: list[str] | None = None
    hypr: str | None = None
    ignore_failure: bool = False


@dataclass
class Result:
    kind: str  # "type" | "say" | "do" | "status" | "cat_status" | "opencode_*"
    text: str = ""
    actions: list[Action] = field(default_factory=list)
    directory: str | None = None


DEFAULT_WAKE_WORDS = ("voice assistant", "assistant")

# Named colors for the den lights and Nanoleaf. White-ish names use a color
# temperature so they look like actual white instead of a saturated RGB mix.
COLOR_TEMPS = {
    "warm": 2700,
    "warm white": 2700,
    "soft white": 3000,
    "white": 4000,
    "cool": 6500,
    "cool white": 6500,
    "daylight": 6500,
}
COLOR_RGB = {
    "red": (255, 0, 0),
    "orange": (255, 96, 0),
    "amber": (255, 176, 0),
    "yellow": (255, 220, 0),
    "lime": (128, 255, 0),
    "green": (0, 255, 0),
    "mint": (64, 255, 180),
    "teal": (0, 220, 180),
    "cyan": (0, 255, 255),
    "blue": (0, 64, 255),
    "purple": (128, 0, 255),
    "violet": (160, 64, 255),
    "magenta": (255, 0, 255),
    "pink": (255, 80, 160),
    "gold": (255, 200, 40),
    "sunset": (255, 96, 64),
}


def _clean_color(value: str) -> str:
    value = value.strip(" .?!,")
    value = re.sub(r"\b(?:please|now|thanks)\b", "", value)
    value = re.sub(r"\s+effect$", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _color_value(name: str) -> tuple[str, Any] | None:
    key = normalize(name)
    if key in COLOR_TEMPS:
        return "temp", COLOR_TEMPS[key]
    if key in COLOR_RGB:
        return "rgb", COLOR_RGB[key]
    if re.fullmatch(r"[0-9a-f]{6}", key):
        return "rgb", (
            int(key[0:2], 16),
            int(key[2:4], 16),
            int(key[4:6], 16),
        )
    return None


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s']", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def default_lock_command() -> list[str]:
    if os.name == "nt":
        return ["rundll32.exe", "user32.dll,LockWorkStation"]
    if sys.platform == "darwin":
        return ["pmset", "displaysleepnow"]
    return ["loginctl", "lock-session"]


def split_wake(text: str, wake_words: list[str] | tuple[str, ...] | None = None) -> tuple[bool, str]:
    """Return (is_command, remainder). A wake word makes it a command."""
    normalized = normalize(text)
    for wake in (wake_words or DEFAULT_WAKE_WORDS):
        wake = normalize(str(wake))
        if not wake:
            continue
        if normalized == wake:
            return True, ""
        if normalized.startswith(wake + " "):
            return True, normalized[len(wake) + 1 :].strip()
    return False, text


def _flatten(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


class Router:
    """Deterministic intents first, optional local LLM second."""

    def __init__(self, config: Config, ha=None, llm=None, resolver=None):
        self.config = config
        self.ha = ha
        self.llm = llm
        self.resolver = resolver

    def route(self, text: str) -> Result:
        normalized = normalize(text)
        if not normalized:
            return Result("say", "I didn't hear anything.")

        result = self._match_opencode(text)
        if result is not None:
            return result

        mode = self._match_mode(normalized)
        if mode is not None:
            return self._mode_result(mode)

        result = self._match_builtin(normalized)
        if result is not None:
            return result

        if self.llm is not None:
            result = self.llm.route(normalized, self)
            if result is not None:
                return result

        # Echo the transcript so a miss sounds like a missing intent, not a
        # hearing failure. This was the difference between "it didn't hear me"
        # and "it heard me but has no action for that."
        snippet = text.strip()
        if len(snippet) > 120:
            snippet = snippet[:117].rstrip() + "..."
        return Result(
            "say",
            f'I heard "{snippet}", but I don\'t have an action for that.',
        )

    # -- OpenCode delegation --------------------------------------------
    def _match_opencode(self, text: str) -> Result | None:
        raw = text.strip()

        start = re.match(
            r"(?is)^(?:jarvis[, ]*)?(?:ask|have|tell|get|use|send)\s+"
            r"(?:opencode|the\s+coding\s+agent|coding\s+agent)"
            r"(?:\s+in\s+(\S+))?(?:\s+to)?\s+(.+)$",
            raw,
        )
        if start and start.group(2).strip():
            return Result(
                "opencode_start",
                start.group(2).strip(),
                directory=start.group(1),
            )

        status = re.match(
            r"(?is)^(?:jarvis[, ]*)?(?:"
            r"(?:what'?s|what is|how'?s|how is)\s+opencode\s+doing|"
            r"(?:check|show)\s+opencode(?:\s+status)?|"
            r"opencode\s+(?:status|progress|doing)"
            r")(?:\s+(job_[a-z0-9]+))?\??$",
            raw,
        )
        if status:
            return Result("opencode_status", status.group(1) or "")

        simple = re.match(r"(?is)^(?:jarvis[, ]*)?opencode[,:]?\s+(.+)$", raw)
        if simple:
            return Result("opencode_start", simple.group(1).strip())

        delegate = re.match(
            r"(?is)^(?:jarvis[, ]*)?delegate\s+(?:this\s+)?to\s+opencode[: ]+(.+)$",
            raw,
        )
        if delegate:
            return Result("opencode_start", delegate.group(1).strip())

        return None

    # -- modes -----------------------------------------------------------
    def _match_mode(self, normalized: str) -> dict[str, Any] | None:
        for mode in self.config.modes:
            name = normalize(str(mode.get("name", "")))
            aliases = [normalize(str(a)) for a in mode.get("aliases", [])]
            candidates = [name, *aliases]
            for candidate in candidates:
                if not candidate:
                    continue
                if normalized == candidate or normalized.startswith(candidate + " "):
                    return mode
        return None

    def _mode_result(self, mode: dict[str, Any]) -> Result:
        actions: list[Action] = []
        for raw in mode.get("actions", []) or []:
            actions.append(
                Action(
                    service=raw.get("service"),
                    target=dict(raw.get("target", {}) or {}),
                    data=dict(raw.get("data", {}) or {}),
                    shell=list(raw["shell"]) if raw.get("shell") else None,
                    hypr=raw.get("hypr"),
                    ignore_failure=bool(raw.get("ignore_failure", False)),
                )
            )
        name = str(mode.get("name", "mode")).title()
        return Result("do", str(mode.get("say") or f"{name} mode."), actions)

    # -- deterministic commands -----------------------------------------
    def _match_builtin(self, normalized: str) -> Result | None:
        if normalized in {
            "lock",
            "lock the desktop",
            "lock my screen",
            "lock the computer",
            "lock the pc",
        }:
            return Result(
                "do",
                "Locking the desktop.",
                [Action(shell=list(self.config.get("system.lock", []) or default_lock_command()))],
            )

        if normalized in {
            "status",
            "system status",
            "what's the gpu doing",
            "whats the gpu doing",
            "how's the system",
            "hows the system",
            "gpu status",
        }:
            return Result("status")

        if normalized in {
            "cat status",
            "how's the cat",
            "hows the cat",
            "litter status",
            "cat litter status",
        }:
            return Result("cat_status")

        if normalized in {"what time is it", "what's the time", "whats the time", "time"}:
            now = datetime.now().astimezone()
            return Result("say", f"It's {now.strftime('%I:%M %p').lstrip('0')}.")

        if normalized in {
            "what's the date",
            "whats the date",
            "what day is it",
            "date",
        }:
            now = datetime.now().astimezone()
            return Result("say", f"It's {now.strftime('%A, %B')} {now.day}.")

        match = re.search(r"(?:go to |switch to )?workspace (\d+)", normalized)
        if match:
            number = match.group(1)
            return Result("do", f"Workspace {number}.", [Action(hypr=f"workspace {number}")])

        match = re.match(r"(?:open|launch|start) (.+)$", normalized)
        if match:
            app = match.group(1).strip()
            apps = self.config.get("apps", {}) or {}
            if app in apps:
                return Result(
                    "do",
                    f"Opening {app}.",
                    [Action(shell=list(apps[app]))],
                )

        steamdeck = self._steamdeck(normalized)
        if steamdeck is not None:
            return steamdeck

        samsung_tv = self._samsung_tv(normalized)
        if samsung_tv is not None:
            return samsung_tv

        appletv = self._appletv(normalized)
        if appletv is not None:
            return appletv

        lights = self._lights(normalized)
        if lights is not None:
            return lights

        nanoleaf = self._nanoleaf(normalized)
        if nanoleaf is not None:
            return nanoleaf

        media = self._media(normalized)
        if media is not None:
            return media

        cat = self._cat(normalized)
        if cat is not None:
            return cat

        return None

    def _lights(self, normalized: str) -> Result | None:
        entities = _flatten(self.config.get("lights.entities", []))
        if not entities:
            return None

        if (
            re.search(
                r"\b(?:turn (?:the )?(?:den )?lights? (?:off|out)|"
                r"turn (?:off|out) (?:the )?(?:den )?lights?)\b",
                normalized,
            )
            or normalized in {"lights off", "kill the lights"}
        ):
            return Result(
                "do",
                "Lights off.",
                [Action(service="light.turn_off", target={"entity_id": entities})],
            )

        if (
            re.search(
                r"\b(?:turn (?:the )?(?:den )?lights? on|"
                r"turn on (?:the )?(?:den )?lights?)\b",
                normalized,
            )
            or normalized == "lights on"
        ):
            return Result(
                "do",
                "Lights on.",
                [Action(service="light.turn_on", target={"entity_id": entities})],
            )

        if "dim" in normalized and "light" in normalized:
            return Result(
                "do",
                "Dimming the lights.",
                [
                    Action(
                        service="light.turn_on",
                        target={"entity_id": entities},
                        data={"brightness_pct": 25},
                    )
                ],
            )

        match = re.search(r"lights? (?:to |at )?(\d{1,3}) ?(?:percent|%)?", normalized)
        if match and ("light" in normalized):
            level = max(1, min(100, int(match.group(1))))
            return Result(
                "do",
                f"Lights at {level} percent.",
                [
                    Action(
                        service="light.turn_on",
                        target={"entity_id": entities},
                        data={"brightness_pct": level},
                    )
                ],
            )

        color_match = re.search(
            r"\b(?:set|make|turn|change)\s+(?:the\s+)?(?:den\s+)?lights?\s+"
            r"(?:to\s+|into\s+)?(.+)$",
            normalized,
        )
        if color_match is None:
            color_match = re.search(
                r"\blights?\s+(?:to\s+|at\s+)?(.+)$", normalized
            )
        if color_match:
            color = _clean_color(color_match.group(1))
            value = _color_value(color)
            if value is not None:
                kind, payload = value
                return Result(
                    "do",
                    f"Lights {color}.",
                    [self._color_action(entity, kind, payload) for entity in entities],
                )
        return None

    def _color_action(self, entity: str, kind: str, payload: Any) -> Action:
        if kind == "temp":
            return Action(
                service="light.turn_on",
                target={"entity_id": entity},
                data={"color_temp_kelvin": int(payload)},
            )

        rgb = list(payload)
        modes: list[str] = []
        if self.ha is not None:
            try:
                state = self.ha.get_state(entity)
                modes = list(
                    (state.get("attributes") or {}).get("supported_color_modes")
                    or []
                )
            except Exception:  # noqa: BLE001 - fall back to rgb
                modes = []
        if "hs" in modes and "rgb" not in modes:
            hue, saturation, _value = colorsys.rgb_to_hsv(
                *(channel / 255.0 for channel in rgb)
            )
            data = {
                "hs_color": [
                    round(hue * 360.0, 1),
                    round(saturation * 100.0, 1),
                ]
            }
        else:
            data = {"rgb_color": rgb}
        return Action(
            service="light.turn_on",
            target={"entity_id": entity},
            data=data,
        )

    # -- Nanoleaf ---------------------------------------------------------
    def _nanoleaf(self, normalized: str) -> Result | None:
        entity = str(self.config.get("lights.nanoleaf", "") or "")
        if not entity:
            return None
        alias = r"(?:nanoleaf|nano leaf|light panels?)"
        if not re.search(rf"\b{alias}\b", normalized):
            return None

        off = (
            re.search(
                rf"\b(?:turn|switch)\s+off\s+(?:the\s+)?{alias}\b",
                normalized,
            )
            or re.search(
                rf"\b(?:turn|switch)\b.*\b{alias}\b.*\boff\b", normalized
            )
            or re.fullmatch(rf"{alias}\s+off", normalized)
        )
        if off:
            return Result(
                "do",
                "Nanoleaf off.",
                [Action(service="light.turn_off", target={"entity_id": entity})],
            )
        on = (
            re.search(
                rf"\b(?:turn|switch)\s+on\s+(?:the\s+)?{alias}\b",
                normalized,
            )
            or re.search(
                rf"\b(?:turn|switch)\b.*\b{alias}\b.*\bon\b", normalized
            )
            or re.fullmatch(rf"{alias}\s+on", normalized)
        )
        if on:
            return Result(
                "do",
                "Nanoleaf on.",
                [Action(service="light.turn_on", target={"entity_id": entity})],
            )

        if "dim" in normalized:
            return Result(
                "do",
                "Dimming the Nanoleaf.",
                [
                    Action(
                        service="light.turn_on",
                        target={"entity_id": entity},
                        data={"brightness_pct": 25},
                    )
                ],
            )

        patterns = [
            rf"^(?:set|put|change|switch|make|turn)\s+(?:the\s+)?{alias}"
            rf"(?:\s+effect)?\s+(?:to\s+|on\s+)?(.+)$",
            rf"^{alias}(?:\s+effect)?\s+(?:to\s+|on\s+)?(.+)$",
        ]
        candidate = ""
        for pattern in patterns:
            match = re.match(pattern, normalized)
            if match:
                candidate = _clean_color(match.group(1))
                break
        if not candidate:
            return None

        color = _color_value(candidate)
        if color is not None:
            kind, payload = color
            return Result(
                "do",
                f"Nanoleaf {candidate}.",
                [self._color_action(entity, kind, payload)],
            )

        effects = self._nanoleaf_effects(entity)
        if not effects:
            return Result(
                "say",
                "The Nanoleaf isn't reporting any effects right now.",
            )
        effect, matches = self._find_effect(candidate, effects)
        if effect:
            return Result(
                "do",
                f"Nanoleaf set to {effect}.",
                [
                    Action(
                        service="light.turn_on",
                        target={"entity_id": entity},
                        data={"effect": effect},
                    )
                ],
            )
        if matches:
            options = ", ".join(matches[:6])
            return Result(
                "say",
                f'I found several Nanoleaf effects matching "{candidate}": {options}.',
            )
        return Result(
            "say",
            f'I don\'t know the Nanoleaf effect "{candidate}".',
        )

    def _nanoleaf_effects(self, entity: str) -> list[str]:
        if self.ha is None:
            return []
        try:
            state = self.ha.get_state(entity)
        except Exception:  # noqa: BLE001
            return []
        return [
            str(effect)
            for effect in (state.get("attributes") or {}).get("effect_list") or []
        ]

    @staticmethod
    def _find_effect(name: str, effects: list[str]) -> tuple[str | None, list[str]]:
        wanted = normalize(name)
        exact = [effect for effect in effects if normalize(effect) == wanted]
        if exact:
            return exact[0], []
        partial = [
            effect
            for effect in effects
            if wanted and wanted in normalize(effect)
        ]
        if len(partial) == 1:
            return partial[0], []
        return None, partial

    @staticmethod
    def _script_action(script: str) -> Action:
        # script.turn_on starts the script and returns immediately; the
        # script.<name> form would block for the wake delay.
        return Action(service="script.turn_on", target={"entity_id": script})

    def _steamdeck(self, normalized: str) -> Result | None:
        scripts = self.config.get("steamdeck", {}) or {}
        if not isinstance(scripts, dict):
            return None
        wake = str(scripts.get("wake", "") or "")
        sleep = str(scripts.get("sleep", "") or "")
        stop = str(scripts.get("stop", "") or "")
        play = str(scripts.get("play_big_walk", "") or "")
        deck = r"(?:the\s+)?(?:steam\s*deck|steamdeck|deck)"
        big_walk = r"(?:the\s+)?(?:game\s+)?big\s+walk"

        if play and (
            re.search(rf"\b(?:play|launch|start|open|run)\s+{big_walk}\b", normalized)
            or re.search(rf"\b{big_walk}\b.*\b(?:on|for)\s+{deck}\b", normalized)
            or re.search(
                rf"\b(?:wake|wake\s+up|turn\s+on|start|boot)\s+{deck}\b"
                rf".*\b{big_walk}\b",
                normalized,
            )
        ):
            waking = bool(re.search(r"\b(?:wake|wake\s+up)\b", normalized))
            return Result(
                "do",
                "Waking the Steam Deck and starting Big Walk."
                if waking
                else "Starting Big Walk on the Steam Deck.",
                [self._script_action(play)],
            )

        if sleep and (
            re.search(rf"\b(?:put|send)\s+{deck}\s+(?:to\s+)?sleep\b", normalized)
            or re.search(rf"\b(?:sleep|suspend)\s+{deck}\b", normalized)
            or re.search(rf"\b{deck}\s+(?:to\s+)?sleep\b", normalized)
        ):
            return Result(
                "do",
                "Putting the Steam Deck to sleep.",
                [self._script_action(sleep)],
            )

        if stop and (
            re.search(
                r"\b(?:stop|quit|close|kill)\s+(?:the\s+)?(?:game|big\s+walk)\b",
                normalized,
            )
            or re.search(
                rf"\b(?:stop|quit|close|kill)\s+{deck}\s+(?:game|big\s+walk)\b",
                normalized,
            )
        ):
            return Result(
                "do",
                "Stopping the game on the Steam Deck.",
                [self._script_action(stop)],
            )

        if wake and re.search(
            rf"\b(?:wake|wake\s+up|turn\s+on|boot|start|launch)\s+{deck}"
            rf"(?:\s+up)?\b",
            normalized,
        ):
            return Result(
                "do",
                "Waking the Steam Deck. Give it about fifteen seconds.",
                [self._script_action(wake)],
            )

        return None

    def _tv_state(self, entity: str) -> str | None:
        if self.ha is None or not entity:
            return None
        try:
            return str(self.ha.get_state(entity).get("state", "") or "")
        except Exception:  # noqa: BLE001 - state lookup must not block routing
            return None

    def _samsung_tv(self, normalized: str) -> Result | None:
        settings = self.config.get("samsung_tv", {}) or {}
        if not isinstance(settings, dict):
            return None
        entity = str(settings.get("entity", "") or "")
        wake = str(settings.get("wake", "") or "")
        toggle = str(settings.get("toggle", "") or "")
        if not entity or not (wake or toggle):
            return None
        tv = r"(?:the\s+)?(?:samsung\s+)?(?:tv|television)"
        if re.fullmatch(rf"{tv}\s+(?:on|off)", normalized):
            normalized = "turn " + normalized

        if toggle and (
            re.search(rf"\btoggle\b.*\b{tv}\b", normalized)
            or re.search(rf"\b{tv}\s+(?:power\s+)?toggle\b", normalized)
            or re.search(
                rf"\b(?:turn|switch)\s+{tv}\s+(?:on|off)\s+or\s+(?:on|off)\b",
                normalized,
            )
        ):
            return Result(
                "do",
                "Toggling the TV.",
                [self._script_action(toggle)],
            )

        if wake and (
            re.search(rf"\b(?:turn|switch)\s+on\s+{tv}\b", normalized)
            or re.search(rf"\b(?:turn|switch)\s+{tv}\s+on\b", normalized)
            or re.search(rf"\bpower\s+on\s+{tv}\b", normalized)
            or re.search(rf"\bwake(?:\s+up)?\s+{tv}\b", normalized)
            or re.search(rf"\bstart\s+{tv}\b", normalized)
        ):
            return Result(
                "do",
                "Turning on the TV.",
                [self._script_action(wake)],
            )

        if toggle and (
            re.search(rf"\b(?:turn|switch)\s+off\s+{tv}\b", normalized)
            or re.search(rf"\b(?:turn|switch)\s+{tv}\s+off\b", normalized)
            or re.search(rf"\bpower\s+off\s+{tv}\b", normalized)
            or re.search(
                rf"\b(?:shut|shut\s+down|shut\s+off|sleep)\s+{tv}\b",
                normalized,
            )
            or re.search(rf"\bput\s+{tv}\s+(?:to\s+)?sleep\b", normalized)
        ):
            # The Power script is a toggle: if the TV is already off, sending
            # key power would wake it via the WoL fallback.
            if self._tv_state(entity) in {"off", "standby"}:
                return Result("say", "The TV is already off.")
            return Result(
                "do",
                "Turning off the TV.",
                [self._script_action(toggle)],
            )

        return None

    def _appletv(
        self,
        normalized: str,
        *,
        service: str | None = None,
        play: bool | None = None,
    ) -> Result | None:
        settings = self.config.get("appletv", {}) or {}
        if not isinstance(settings, dict):
            return None
        entity = str(settings.get("entity", "") or "")
        shows = settings.get("shows", {}) or {}
        if not entity or not isinstance(shows, dict):
            return None

        tv = r"(?:(?:the|my)\s+)?(?:apple\s*tv|appletv|apple\s+television)"
        match = re.search(
            rf"\b(?P<verb>go\s+to|switch\s+to|put\s+on|open|play|watch|start|launch)\s+"
            rf"(?:(?:the\s+)?show\s+)?(?P<title>.+?)\s+"
            rf"(?:on|from)\s+{tv}\b",
            normalized,
        )
        if not match:
            return None

        title = match.group("title").strip()
        if not title:
            return None
        if play is None:
            play = match.group("verb") in {"play", "watch"}
        title, parsed_service = self._split_appletv_service(title)
        service = service or parsed_service

        show: str | None = None
        url: str | None = None
        play_url: str | None = None
        service_name: str | None = None

        # The local catalog is a fast, reliable override. When the user asks
        # for a specific service, let the resolver choose that service instead.
        if service is None:
            show, url, play_url, matches = self._find_appletv_show(shows, title)
            if show is None and matches:
                options = ", ".join(matches[:6])
                return Result(
                    "say",
                    f'I found several Apple TV shows matching "{title}": {options}.',
                )
            if show is not None and play and play_url:
                url = play_url
        else:
            matches = []

        if show is None and self.resolver is not None:
            deep: DeepLink | None = self.resolver.resolve(
                title,
                service=service,
                play=bool(play),
            )
            if deep is not None:
                show = deep.title
                url = deep.url
                service_name = deep.service

        if show is None:
            if self.resolver is not None:
                if service:
                    return Result(
                        "say",
                        f'I couldn\'t find "{title}" on {service} with an Apple TV deep link.',
                    )
                return Result(
                    "say",
                    f'I couldn\'t find a streaming deep link for "{title}".',
                )
            return Result(
                "say",
                f'I don\'t have a deep link for "{title}" on the Apple TV yet.',
            )
        if not url:
            return Result("say", f'"{show}" has no deep link configured.')

        actions: list[Action] = []
        if self._tv_state(entity) in {"off", "standby"}:
            actions.append(
                Action(
                    service="media_player.turn_on",
                    target={"entity_id": entity},
                    ignore_failure=True,
                )
            )
            delay = float(settings.get("wake_delay", 2) or 0)
            if delay > 0:
                actions.append(Action(shell=["sleep", f"{delay:g}"]))

        actions.append(
            Action(
                service="media_player.play_media",
                target={"entity_id": entity},
                data={
                    "media_content_type": "url",
                    "media_content_id": url,
                },
            )
        )
        if service_name and service_name.lower().startswith("apple tv"):
            speech = f"Opening {show} on Apple TV."
        elif service_name:
            speech = f"Opening {show} on {service_name} on the Apple TV."
        else:
            speech = f"Opening {show} on the Apple TV."
        return Result("do", speech, actions)

    @staticmethod
    def _split_appletv_service(title: str) -> tuple[str, str | None]:
        match = re.match(r"(.+?)\s+(?:on|from)\s+(.+)$", title)
        if not match:
            return title, None
        suffix = normalize(match.group(2))
        aliases = {
            "peacock": "Peacock",
            "hulu": "Hulu",
            "disney plus": "Disney+",
            "disney+": "Disney+",
            "max": "Max",
            "hbo max": "Max",
            "prime video": "Prime Video",
            "amazon prime": "Prime Video",
            "apple tv plus": "Apple TV+",
            "apple tv+": "Apple TV+",
            "netflix": "Netflix",
            "paramount plus": "Paramount+",
            "paramount+": "Paramount+",
        }
        for alias, service in aliases.items():
            if suffix == alias or suffix.startswith(alias + " "):
                return match.group(1).strip(), service
        return title, None

    @staticmethod
    def _find_appletv_show(
        shows: dict[str, Any], title: str
    ) -> tuple[str | None, str | None, str | None, list[str]]:
        wanted = normalize(title)
        if not wanted:
            return None, None, None, []

        exact: list[tuple[str, Any]] = []
        partial: list[tuple[str, Any]] = []
        for key, value in shows.items():
            if not isinstance(value, (str, dict)):
                continue
            names = [normalize(key)]
            if isinstance(value, dict):
                names.extend(
                    normalize(str(alias))
                    for alias in value.get("aliases", []) or []
                )
            if wanted in names:
                exact.append((key, value))
            elif any(wanted in name or name in wanted for name in names if name):
                partial.append((key, value))

        selected = exact if exact else partial
        if len(selected) != 1:
            return (
                None,
                None,
                None,
                [str(key) for key, _value in selected],
            )

        key, value = selected[0]
        if isinstance(value, str):
            return key, value, None, []
        display = str(value.get("name", key))
        url = str(value.get("url", "") or "")
        play_url = str(value.get("play_url", "") or "") or None
        return display, url, play_url, []

    def _media(self, normalized: str) -> Result | None:
        entity = str(self.config.get("media.entity", ""))
        if not entity:
            return None
        actions = {
            "pause": ("media_pause", "Paused."),
            "resume": ("media_play", "Playing."),
            "play": ("media_play", "Playing."),
            "next": ("media_next_track", "Next track."),
            "next track": ("media_next_track", "Next track."),
            "previous": ("media_previous_track", "Previous track."),
            "previous track": ("media_previous_track", "Previous track."),
            "volume up": ("volume_up", "Volume up."),
            "volume down": ("volume_down", "Volume down."),
            "mute": ("volume_mute", "Muted."),
            "unmute": ("volume_mute", "Unmuted."),
        }
        for phrase, (service, say) in actions.items():
            if normalized == phrase or normalized.endswith(" " + phrase):
                data: dict[str, Any] = {"entity_id": entity}
                if phrase == "unmute":
                    data["is_volume_muted"] = False
                elif phrase == "mute":
                    data["is_volume_muted"] = True
                return Result(
                    "do",
                    say,
                    [Action(service=f"media_player.{service}", data=data)],
                )
        if normalized in {"play pause", "pause play", "toggle playback"}:
            return Result(
                "do",
                "Toggled playback.",
                [Action(service="media_player.media_play_pause", data={"entity_id": entity})],
            )
        return None

    def _cat(self, normalized: str) -> Result | None:
        feeder = str(self.config.get("cat.feeder", ""))
        portions = int(self.config.get("cat.feed_portions", 1))
        if feeder and (
            "feed the cat" in normalized
            or "feed the cats" in normalized
            or "feed cat" in normalized
        ):
            return Result(
                "do",
                "Feeding the cat.",
                [
                    Action(
                        service="number.set_value",
                        data={"entity_id": feeder, "value": portions},
                    )
                ],
            )

        litter = str(self.config.get("cat.litter_clean", ""))
        if litter and (
            "clean the litter" in normalized
            or "clean the cat box" in normalized
            or "clean litter" in normalized
        ):
            return Result(
                "do",
                "Cleaning the litter box.",
                [Action(service="button.press", data={"entity_id": litter})],
            )
        return None

    # -- tools exposed to the local LLM ---------------------------------
    def tool_result(self, tool: str, args: dict[str, Any]) -> Result | None:
        if tool == "set_mode":
            name = str(args.get("mode", ""))
            for mode in self.config.modes:
                if normalize(str(mode.get("name", ""))) == normalize(name):
                    return self._mode_result(mode)
            return None

        if tool == "lock_desktop":
            return self._match_builtin("lock the desktop")

        if tool == "system_status":
            return Result("status")

        if tool == "cat_status":
            return Result("cat_status")

        if tool == "feed_cat":
            return self._match_builtin("feed the cat")

        if tool == "clean_litter":
            return self._match_builtin("clean the litter box")

        if tool == "open_app":
            return self._match_builtin(f"open {args.get('app', '')}")

        if tool == "workspace":
            return self._match_builtin(f"workspace {args.get('number', '')}")

        if tool == "lights":
            action = str(args.get("action", ""))
            if action == "color":
                color = str(args.get("color", "") or "").strip()
                if not color:
                    return None
                return self._lights(f"set the lights to {color}")
            return self._match_builtin(f"lights {action}")

        if tool == "nanoleaf_effect":
            effect = str(args.get("effect", "") or "").strip()
            if not effect:
                return None
            return self._nanoleaf(f"set the nanoleaf to {effect}")

        if tool == "media":
            action = str(args.get("action", "")).replace("_", " ")
            return self._match_builtin(action)

        if tool == "samsung_tv":
            action = str(args.get("action", "")).replace("-", "_")
            phrases = {
                "on": "turn on the tv",
                "off": "turn off the tv",
                "toggle": "toggle the tv",
            }
            phrase = phrases.get(action)
            if not phrase:
                return None
            return self._samsung_tv(phrase)

        if tool == "apple_tv":
            title = str(args.get("title", "") or "").strip()
            if not title:
                return None
            action = str(args.get("action", "open") or "open")
            service = str(args.get("service", "") or "").strip() or None
            verb = "play" if action == "play" else "go to"
            return self._appletv(
                normalize(f"{verb} {title} on the apple tv"),
                service=service,
                play=action == "play",
            )

        if tool == "steam_deck":
            action = str(args.get("action", "")).replace("-", "_")
            phrases = {
                "wake": "wake up the steam deck",
                "play_big_walk": "play big walk on the steam deck",
                "sleep": "put the steam deck to sleep",
                "stop": "stop the game on the steam deck",
            }
            phrase = phrases.get(action)
            if not phrase:
                return None
            return self._steamdeck(normalize(phrase))

        if tool == "opencode_start":
            prompt = str(args.get("prompt", "") or "").strip()
            if not prompt:
                return None
            directory = args.get("directory")
            return Result(
                "opencode_start",
                prompt,
                directory=str(directory) if directory else None,
            )

        if tool == "opencode_status":
            return Result("opencode_status", str(args.get("job_id", "") or ""))

        return None
