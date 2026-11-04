"""Live routing catalog for the local Voice Assistant engine.

Everything here is read from the Voice Assistant data and engine at runtime: config.toml,
router.py, llm.py, README.md, tests, and Home Assistant's Nanoleaf effect list.
The dashboard never keeps a second hand-written command list.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parent
JARVIS_ROOT = BACKEND
DATA_ROOT = Path(os.environ.get("VOICE_DATA_ROOT") or (Path(os.environ.get("ORQELLA_HOST_HOME") or Path.home()) / ".local/share/orqella/modules/voice-assistant/data")).expanduser()
JARVIS_CONFIG_FILE = DATA_ROOT / "config.toml"
JARVIS_ROUTER_FILE = BACKEND / "engine" / "router.py"
JARVIS_LLM_FILE = BACKEND / "engine" / "llm.py"
JARVIS_README_FILE = BACKEND.parent / "README.md"

_JARVIS_CAP_CACHE: dict[str, Any] = {"at": 0.0, "data": None}
_JARVIS_ROUTE_SCRIPT = r"""
import json
import sys
from dataclasses import asdict
from engine.cli import build_ha
from engine.config import load
from engine.router import Router

payload = json.loads(sys.stdin.read())
config = load()
llm = None
if payload.get("llm"):
    from engine.llm import LLM
    llm = LLM(config)
try:
    ha = build_ha(config)
except Exception:
    ha = None
result = Router(config, ha=ha, llm=llm).route(str(payload.get("text") or ""))
print(json.dumps({
    "kind": result.kind,
    "say": result.text,
    "directory": result.directory,
    "actions": [asdict(action) for action in result.actions],
}))
"""
_JARVIS_EFFECTS_SCRIPT = r"""
import json
from engine.config import load
from engine.cli import build_ha
from engine.router import Router

config = load()
entity = str(config.get("lights.nanoleaf", "") or "")
if not entity:
    print("[]")
else:
    print(json.dumps(Router(config, ha=build_ha(config))._nanoleaf_effects(entity)))
"""
_JARVIS_BATCH_SCRIPT = r"""
import json
import sys
from engine.config import load
from engine.router import Router

phrases = json.loads(sys.stdin.read())
router = Router(load())
out = []
for phrase in phrases:
    result = router.route(str(phrase))
    out.append({
        "phrase": str(phrase),
        "kind": result.kind,
        "say": result.text,
        "actions": len(result.actions),
    })
print(json.dumps(out))
"""


def _jarvis_norm(value: str) -> str:
    return " ".join(str(value or "").lower().split()).strip(" .?!")


def _jarvis_action_label(action: dict[str, Any]) -> str:
    if not isinstance(action, dict):
        return str(action)
    if action.get("service"):
        target = action.get("target") or {}
        entity = target.get("entity_id")
        bits = [str(action["service"])]
        if isinstance(entity, list):
            bits.append(", ".join(str(item) for item in entity))
        elif entity:
            bits.append(str(entity))
        data = action.get("data") or {}
        for key, value in list(data.items())[:4]:
            bits.append(f"{key}={value}")
        return " · ".join(bits)
    if action.get("shell"):
        return "$ " + " ".join(str(part) for part in action["shell"])
    if action.get("hypr"):
        return "hyprctl dispatch " + str(action["hypr"])
    return "noop"


def _jarvis_load_config() -> dict[str, Any]:
    try:
        return tomllib.loads(JARVIS_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _jarvis_ast_module(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None


def _jarvis_tools() -> list[dict[str, Any]]:
    tree = _jarvis_ast_module(JARVIS_LLM_FILE)
    if tree is None:
        return []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value_node = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value_node = node.value
        else:
            continue
        if not any(isinstance(target, ast.Name) and target.id == "TOOLS" for target in targets):
            continue
        if value_node is None:
            return []
        try:
            value = ast.literal_eval(value_node)
        except (ValueError, SyntaxError):
            return []
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _jarvis_source_meta() -> dict[str, Any]:
    tree = _jarvis_ast_module(JARVIS_ROUTER_FILE)
    if tree is None:
        return {"exacts": [], "media": [], "colors": [], "patterns": {}, "families": []}

    parents = {
        child: node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }

    def owner(node: ast.AST) -> str:
        while node is not None and not isinstance(node, ast.FunctionDef):
            node = parents.get(node)
        return node.name if isinstance(node, ast.FunctionDef) else ""

    exacts: list[dict[str, Any]] = []
    seen_exacts: set[tuple[str, tuple[str, ...]]] = set()
    patterns: dict[str, int] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "re"
                and node.func.attr in {"search", "match", "fullmatch"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                name = owner(node)
                if name:
                    patterns[name] = patterns.get(name, 0) + 1

        if not isinstance(node, ast.If):
            continue
        phrases: set[str] = set()
        for comparison in ast.walk(node.test):
            if not isinstance(comparison, ast.Compare):
                continue
            if not (isinstance(comparison.left, ast.Name) and comparison.left.id == "normalized"):
                continue
            for comparator in comparison.comparators:
                if isinstance(comparator, (ast.Set, ast.List, ast.Tuple)):
                    phrases.update(
                        item.value
                        for item in comparator.elts
                        if isinstance(item, ast.Constant) and isinstance(item.value, str)
                    )
                elif isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                    phrases.add(comparator.value)
        if not phrases:
            continue
        kind = ""
        say = ""
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "Result":
                if sub.args and isinstance(sub.args[0], ast.Constant):
                    kind = str(sub.args[0].value)
                if len(sub.args) > 1 and isinstance(sub.args[1], ast.Constant):
                    say = str(sub.args[1].value)
                break
        function = owner(node)
        key = (function, tuple(sorted(phrases)))
        if key in seen_exacts:
            continue
        seen_exacts.add(key)
        exacts.append({
            "function": function,
            "phrases": sorted(phrases),
            "kind": kind,
            "say": say,
        })

    media: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_media":
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign):
                continue
            if not any(isinstance(target, ast.Name) and target.id == "actions" for target in sub.targets):
                continue
            try:
                value = ast.literal_eval(sub.value)
            except (ValueError, SyntaxError):
                continue
            if isinstance(value, dict):
                for phrase, spec in value.items():
                    if isinstance(spec, (list, tuple)) and len(spec) >= 2:
                        media.append({
                            "phrase": str(phrase),
                            "service": str(spec[0]),
                            "say": str(spec[1]),
                        })

    colors: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id in {"COLOR_RGB", "COLOR_TEMPS"}
            for target in node.targets
        ):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, dict):
            colors.extend(str(name) for name in value.keys())

    families = set(patterns) | {item["function"] for item in exacts}
    return {
        "exacts": exacts,
        "media": media,
        "colors": list(dict.fromkeys(colors)),
        "patterns": patterns,
        "families": sorted(name for name in families if name),
    }


def _jarvis_tested_phrases() -> list[str]:
    path = JARVIS_ROOT / "tests" / "test_router.py"
    tree = _jarvis_ast_module(path) if path.exists() else None
    if tree is None:
        return []
    phrases: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "route"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            if node.args[0].value not in phrases:
                phrases.append(node.args[0].value)
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            values = [
                element.value
                for element in node.iter.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
            if values and any(
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "route"
                for sub in ast.walk(node)
            ):
                for value in values:
                    if value not in phrases:
                        phrases.append(value)
    return phrases


def _jarvis_route_batch(phrases: list[str]) -> list[dict[str, Any]]:
    if not phrases or not JARVIS_ROOT.is_dir():
        return []
    env = dict(os.environ)
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(JARVIS_ROOT) + (":" + current if current else "")
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _JARVIS_BATCH_SCRIPT],
            input=json.dumps(phrases),
            text=True,
            capture_output=True,
            cwd=str(JARVIS_ROOT),
            env=env,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    for line in reversed((proc.stdout or "").strip().splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, list):
            return [entry for entry in payload if isinstance(entry, dict)]
    return []


def _jarvis_nanoleaf_effects() -> list[str]:
    if not JARVIS_ROOT.is_dir():
        return []
    env = dict(os.environ)
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(JARVIS_ROOT) + (":" + current if current else "")
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _JARVIS_EFFECTS_SCRIPT],
            text=True,
            capture_output=True,
            cwd=str(JARVIS_ROOT),
            env=env,
            timeout=6,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    for line in reversed((proc.stdout or "").strip().splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, list):
            return [str(value) for value in payload if str(value).strip()]
    return []


def _jarvis_readme_examples() -> list[str]:
    try:
        text = JARVIS_README_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    import re as _re
    phrases = _re.findall(r'^\s*-\s+[“"]([^”"]+)[”"]', text, _re.MULTILINE)
    out: list[str] = []
    for phrase in phrases:
        phrase = phrase.strip()
        if phrase.lower().startswith("jarvis, "):
            phrase = phrase[8:].strip()
        if phrase and phrase not in out:
            out.append(phrase)
    return out


def voice_capabilities(force: bool = False) -> dict[str, Any]:
    now = time.time()
    cached = _JARVIS_CAP_CACHE.get("data")
    if not force and cached and now - float(_JARVIS_CAP_CACHE.get("at") or 0) < 12:
        return cached

    if not JARVIS_ROOT.is_dir():
        return {"error": "Voice Assistant is not installed", "groups": [], "generated": now}

    config = _jarvis_load_config()
    source = _jarvis_source_meta()
    tools = _jarvis_tools()
    nanoleaf_effects: list[str] = []
    lights_config = config.get("lights") or {}
    if isinstance(lights_config, dict) and lights_config.get("nanoleaf"):
        nanoleaf_effects = _jarvis_nanoleaf_effects()
    groups: list[dict[str, Any]] = []
    seen: set[str] = set()

    def item(
        name: str,
        description: str,
        phrases: list[str] | None = None,
        detail: str = "",
        *,
        kind: str = "command",
        delete: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean: list[str] = []
        for phrase in phrases or []:
            phrase = str(phrase).strip()
            if phrase and phrase not in clean:
                clean.append(phrase)
        return {
            "name": str(name),
            "description": str(description),
            "phrases": clean,
            "detail": str(detail),
            "kind": kind,
            "delete": delete,
        }

    def add_group(
        group_id: str,
        label: str,
        glyph: str,
        description: str,
        items: list[dict[str, Any]],
        *,
        source_label: str = "config",
    ) -> None:
        items = [entry for entry in items if entry]
        if not items:
            return
        groups.append({
            "id": group_id,
            "label": label,
            "glyph": glyph,
            "description": description,
            "source": source_label,
            "items": items,
        })
        for entry in items:
            for phrase in entry.get("phrases", []):
                seen.add(_jarvis_norm(phrase))

    modes = [
        mode for mode in (config.get("mode") or [])
        if isinstance(mode, dict)
    ]
    add_group(
        "modes",
        "House modes",
        "🎬",
        "Named multi-action routines from config.toml.",
        [
            item(
                str(mode.get("name") or "mode").title(),
                str(mode.get("say") or f"{str(mode.get('name') or 'mode').title()} mode."),
                [str(mode.get("name") or "")] + [str(alias) for alias in (mode.get("aliases") or [])],
                " · ".join(_jarvis_action_label(action) for action in (mode.get("actions") or []) if isinstance(action, dict)),
                kind="mode",
                delete={"type": "mode", "name": str(mode.get("name"))} if mode.get("name") else None,
            )
            for mode in modes
        ],
        source_label="config modes",
    )

    apps = config.get("apps") or {}
    if isinstance(apps, dict):
        add_group(
            "apps",
            "Applications",
            "🚀",
            "Launch commands from [apps] in config.toml.",
            [
                item(
                    str(app).title(),
                    f"Open {app}.",
                    [f"open {app}", f"launch {app}", f"start {app}"],
                    " ".join(str(part) for part in command) if isinstance(command, list) else str(command),
                    kind="app",
                    delete={"type": "app", "key": str(app)},
                )
                for app, command in apps.items()
            ],
            source_label="config apps",
        )

    lights = config.get("lights") or {}
    if isinstance(lights, dict):
        entities = lights.get("entities") or []
        if isinstance(entities, str):
            entities = [entities]
        nanoleaf = str(lights.get("nanoleaf") or "")
        light_items: list[dict[str, Any]] = []
        if entities:
            light_items.extend([
                item(
                    "Lights on / off",
                    f"Turn all configured den lights on or off ({len(entities)} entities).",
                    [
                        "turn on the lights", "turn on the den lights", "lights on", "turn the lights on",
                        "turn off the lights", "turn off the den lights", "lights off", "turn the lights off",
                        "kill the lights",
                    ],
                    ", ".join(str(entity) for entity in entities),
                ),
                item(
                    "Dim the lights",
                    "Set every den light to 25% brightness.",
                    ["dim the lights"],
                    "light.turn_on · brightness_pct=25 · " + ", ".join(str(entity) for entity in entities),
                ),
                item(
                    "Set brightness",
                    "Set every den light to a percentage.",
                    ["lights 50", "lights at 25 percent", "set the lights to 75%"],
                    "light.turn_on · brightness_pct=<requested> · " + ", ".join(str(entity) for entity in entities),
                ),
            ])
            colors = source.get("colors") or []
            if colors:
                light_items.append(item(
                    "Set a color",
                    "Set every den light to a named color or hex value.",
                    [f"set the lights to {color}" for color in colors],
                    "Named colors: " + ", ".join(colors),
                ))
        if nanoleaf:
            effect_phrases = [f"set the nanoleaf to {effect}" for effect in nanoleaf_effects]
            light_items.append(item(
                "Nanoleaf",
                "Control the light panels; effects are read from Home Assistant's effect_list.",
                [
                    "turn on the nanoleaf", "turn off the nanoleaf", "dim the nanoleaf",
                    "set the nanoleaf to red",
                ] + (effect_phrases or [
                    "set the nanoleaf to Ocean",
                    "put the nanoleaf on Northern Lights",
                ]),
                nanoleaf + (f" · {len(nanoleaf_effects)} effects" if nanoleaf_effects else ""),
            ))
        add_group(
            "lights",
            "Lights & Nanoleaf",
            "💡",
            "Deterministic light routes, including named colors and HA effects.",
            light_items,
            source_label="config + router",
        )

    media_entity = str((config.get("media") or {}).get("entity") or "")
    if media_entity:
        media_phrases = [entry["phrase"] for entry in source.get("media") or []]
        media_phrases.extend(["play pause", "pause play", "toggle playback"])
        services = [entry["service"] for entry in source.get("media") or []]
        add_group(
            "media",
            "Media",
            "🎵",
            f"Playback controls for {media_entity}.",
            [
                item(
                    "Playback controls",
                    "Pause, resume, skip tracks, change volume, or mute.",
                    media_phrases,
                    ", ".join(dict.fromkeys(services)),
                )
            ],
            source_label="config + router",
        )

    cat = config.get("cat") or {}
    if isinstance(cat, dict):
        cat_items: list[dict[str, Any]] = []
        if cat.get("feeder"):
            cat_items.append(item(
                "Feed the cat",
                f"Dispense {cat.get('feed_portions', 1)} portion(s) from {cat['feeder']}.",
                ["feed the cat", "feed the cats", "feed cat"],
                f"number.set_value · {cat['feeder']} · value={cat.get('feed_portions', 1)}",
            ))
        if cat.get("litter_clean"):
            cat_items.append(item(
                "Clean the litter box",
                f"Run the cleaning cycle on {cat['litter_clean']}.",
                ["clean the litter box", "clean the litter", "clean the cat box", "clean litter"],
                f"button.press · {cat['litter_clean']}",
            ))
        if cat.get("weight") or cat.get("visits"):
            cat_items.append(item(
                "Cat status",
                "Report the cat's weight and litter box visits.",
                ["cat status", "how's the cat", "litter status", "cat litter status"],
                " · ".join(str(cat[key]) for key in ("weight", "visits") if cat.get(key)),
            ))
        add_group("cat", "Cat", "🐈", "Pet feeder, litter box and status routes.", cat_items, source_label="config cat")

    appletv = config.get("appletv") or {}
    if isinstance(appletv, dict) and appletv.get("entity"):
        shows = appletv.get("shows") or {}
        show_items: list[dict[str, Any]] = []
        for key, entry in shows.items():
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("name") or key)
            phrases = [f"go to {title} on the Apple TV"]
            if entry.get("play_url"):
                phrases.append(f"watch {title} on the Apple TV")
            show_items.append(item(
                title,
                f"Open {title} on the Apple TV via the local catalog.",
                phrases,
                f"catalog: {key}",
                delete={"type": "appletv_show", "key": str(key)},
            ))
        resolver = appletv.get("resolver") or {}
        if resolver.get("enabled"):
            show_items.append(item(
                "Resolve another title",
                "Unknown titles are resolved through JustWatch, with an optional service hint.",
                [
                    "go to The Office on the Apple TV",
                    "watch The Bear on Hulu on the Apple TV",
                    "go to Severance on the Apple TV",
                ],
                "preferred: " + ", ".join(str(x) for x in (resolver.get("preferred") or [])),
            ))
        add_group(
            "appletv",
            "Apple TV",
            "📺",
            f"Deep links for {appletv['entity']}; wakes the TV first if needed.",
            show_items,
            source_label="config + resolver",
        )

    samsung = config.get("samsung_tv") or {}
    if isinstance(samsung, dict) and samsung.get("entity"):
        add_group(
            "samsung",
            "Samsung TV",
            "📺",
            "Power control through the configured Home Assistant scripts.",
            [
                item(
                    "Power",
                    "Wake, toggle or turn off the Samsung TV.",
                    ["turn on the TV", "wake up the Samsung TV", "TV on", "toggle the TV", "switch the TV on or off", "turn off the TV", "TV off"],
                    " · ".join(str(samsung.get(key)) for key in ("wake", "toggle") if samsung.get(key)),
                )
            ],
            source_label="config samsung_tv",
        )

    deck = config.get("steamdeck") or {}
    if isinstance(deck, dict) and any(deck.get(key) for key in ("wake", "sleep", "stop", "play_big_walk")):
        add_group(
            "steamdeck",
            "Steam Deck",
            "🎮",
            "Home Assistant scripts for the Steam Deck.",
            [
                item("Wake", "Wake the Steam Deck.", ["wake up the Steam Deck", "wake the deck", "turn on the Steam Deck", "start the Steamdeck"], str(deck.get("wake") or "")),
                item("Sleep", "Put the Steam Deck to sleep.", ["put the Steam Deck to sleep", "sleep the Steam Deck"], str(deck.get("sleep") or "")),
                item("Stop the game", "Stop the current game.", ["stop the game", "stop the game on the Steam Deck", "quit the game"], str(deck.get("stop") or "")),
                item("Play Big Walk", "Launch Big Walk, waking the Deck first when needed.", ["launch Big Walk", "play Big Walk on the Steam Deck", "start Big Walk"], str(deck.get("play_big_walk") or "")),
            ],
            source_label="config steamdeck",
        )

    system_items: list[dict[str, Any]] = []
    for entry in source.get("exacts") or []:
        if entry.get("function") != "_match_builtin":
            continue
        phrases = [str(phrase) for phrase in entry.get("phrases") or []]
        if not phrases:
            continue
        if any(_jarvis_norm(phrase) in seen for phrase in phrases):
            continue
        kind = str(entry.get("kind") or "say")
        say = str(entry.get("say") or "").strip()
        if kind == "status":
            name = "System status"
        elif kind == "cat_status":
            name = "Cat status"
        elif say:
            name = say.rstrip(".")
        elif any("time" in _jarvis_norm(phrase) for phrase in phrases):
            name = "Time"
        elif any("date" in _jarvis_norm(phrase) or "day" in _jarvis_norm(phrase) for phrase in phrases):
            name = "Date"
        else:
            name = phrases[0]
        description = {
            "status": "Report GPU, CPU, memory and uptime.",
            "cat_status": "Report the cat's weight and litter box visits.",
        }.get(kind, f"Deterministic {kind} route.")
        detail = f"kind: {kind}"
        if name == "Locking the desktop":
            detail = " ".join(str(part) for part in ((config.get("system") or {}).get("lock") or []))
            description = "Lock the desktop."
        elif name == "System status":
            detail = "Reads CPU, GPU, memory and uptime."
        elif name == "Time":
            detail = "Speaks the current time."
        elif name == "Date":
            detail = "Speaks the current date."
        system_items.append(item(name, description, phrases, detail))
    system_items.append(item(
        "Workspace",
        "Switch to a Hyprland workspace.",
        ["go to workspace 3", "workspace 3", "switch to workspace 5"],
        "hyprctl dispatch workspace N",
    ))
    add_group(
        "system",
        "Desktop & system",
        "🖥",
        "Lock, status, time, date and workspace routes.",
        system_items,
        source_label="router _match_builtin",
    )

    opencode = config.get("opencode") or {}
    add_group(
        "opencode",
        "OpenCode delegation",
        "🤖",
        "Hand coding work to OpenCode or check its jobs.",
        [
            item(
                "Start a session",
                "Ask OpenCode to do a coding task.",
                [
                    "ask opencode to add a test for the parser",
                    "opencode fix the failing build",
                    "delegate this to opencode: add a parser test",
                ],
                " · ".join(str(opencode.get(key)) for key in ("model", "default_dir") if opencode.get(key)),
            ),
            item(
                "Check jobs",
                "Report the status of OpenCode jobs started by Voice Assistant.",
                ["what's opencode doing?", "opencode status", "show opencode status"],
                "Reads Voice Assistant's OpenCode job state.",
            ),
        ],
        source_label="config opencode + router",
    )

    detected: list[dict[str, Any]] = []
    for entry in source.get("exacts") or []:
        if entry.get("function") == "_match_builtin":
            continue
        phrases = [str(phrase) for phrase in entry.get("phrases") or []]
        if not phrases or all(_jarvis_norm(phrase) in seen for phrase in phrases):
            continue
        detected.append(item(
            str(entry.get("say") or phrases[0]).rstrip("."),
            f"Exact route in router.{entry.get('function') or 'route'}.",
            phrases,
            f"kind: {entry.get('kind') or 'say'}",
        ))
        for phrase in phrases:
            seen.add(_jarvis_norm(phrase))
    add_group(
        "detected",
        "Other exact routes",
        "⌁",
        "Exact matches detected directly in router.py.",
        detected,
        source_label="router source",
    )

    tool_items: list[dict[str, Any]] = []
    for tool in tools:
        schema = tool.get("parameters") or {}
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        details = []
        for name, prop in properties.items():
            label = str(name)
            if name in required:
                label += "*"
            enum = (prop or {}).get("enum") if isinstance(prop, dict) else None
            if enum:
                label += " (" + ", ".join(str(value) for value in enum) + ")"
            details.append(label)
        tool_items.append(item(
            str(tool.get("name") or "tool"),
            str(tool.get("description") or "Structured model tool."),
            [],
            ", ".join(details),
            kind="tool",
        ))
    add_group(
        "tools",
        "Model fallback tools",
        "🧰",
        "Structured actions the local model can choose for free-form phrasing.",
        tool_items,
        source_label="jarvis.llm.TOOLS",
    )

    extra: list[dict[str, Any]] = []
    for phrase in _jarvis_readme_examples():
        if _jarvis_norm(phrase) in seen:
            continue
        seen.add(_jarvis_norm(phrase))
        extra.append(item(phrase, "Documented example from the Voice Assistant README.", [phrase], kind="example"))
    add_group(
        "examples",
        "Documented examples",
        "📚",
        "Commands listed in the Voice Assistant README that are not already covered above.",
        extra,
        source_label="README",
    )

    candidates = [
        phrase for phrase in _jarvis_tested_phrases()
        if _jarvis_norm(phrase) not in seen
    ]
    tested_items: list[dict[str, Any]] = []
    for route in _jarvis_route_batch(candidates):
        phrase = str(route.get("phrase") or "").strip()
        kind = str(route.get("kind") or "")
        say = str(route.get("say") or "").strip()
        if not phrase or kind not in {"do", "status", "cat_status", "opencode_start", "opencode_status"}:
            continue
        if "job_abc123" in phrase or "~/Work/foo" in phrase:
            continue
        if any(marker in say for marker in (
            "I don't have an action", "I don't know", "isn't reporting",
            "already off", "found several",
        )):
            continue
        tested_items.append(item(
            phrase,
            "Phrasing covered by Voice Assistant's routing tests.",
            [phrase],
            f"{kind}" + (f" · {say}" if say else ""),
            kind="tested",
        ))
        seen.add(_jarvis_norm(phrase))
    add_group(
        "tested",
        "Tested phrasings",
        "🧪",
        "Additional commands covered by Voice Assistant's routing tests.",
        tested_items,
        source_label="tests/test_router.py",
    )

    result = {
        "generated": now,
        "root": str(JARVIS_ROOT),
        "config": str(JARVIS_CONFIG_FILE),
        "groups": groups,
        "llm": {
            "enabled": bool((config.get("llm") or {}).get("enabled")),
            "model": str((config.get("llm") or {}).get("model") or ""),
        },
        "tools": len(tools),
        "route_families": len(source.get("families") or []),
        "source_patterns": sum((source.get("patterns") or {}).values()),
        "phrase_count": sum(len(entry.get("phrases") or []) for group in groups for entry in group["items"]),
    }
    _JARVIS_CAP_CACHE.update({"at": now, "data": result})
    return result


def invalidate_cache() -> None:
    _JARVIS_CAP_CACHE.update({"at": 0.0, "data": None})


def voice_route_preview(text: str, use_llm: bool = False) -> dict[str, Any]:
    text = str(text or "").strip()
    if not text:
        return {"error": "empty phrase"}
    if len(text) > 1000:
        return {"error": "phrase is too long"}
    if not JARVIS_ROOT.is_dir():
        return {"error": "Voice Assistant is not installed"}
    env = dict(os.environ)
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(JARVIS_ROOT) + (":" + current if current else "")
    timeout = 35 if use_llm else 12
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _JARVIS_ROUTE_SCRIPT],
            input=json.dumps({"text": text, "llm": bool(use_llm)}),
            text=True,
            capture_output=True,
            cwd=str(JARVIS_ROOT),
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": "Voice Assistant route timed out"}
    except OSError as exc:
        return {"error": str(exc)}
    raw = (proc.stdout or "").strip()
    for line in reversed(raw.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payload["phrase"] = text
            return payload
    return {
        "error": (proc.stderr or raw or "Voice Assistant returned no JSON").strip()[-2000:],
        "phrase": text,
    }
