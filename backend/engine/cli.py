from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import config as config_module
from .actions import Executor
from .audio import AudioError, Recording
from .config import Config, STATE_DIR
from .deeplinks import JustWatchResolver
from .ha import HA, HAError
from .llm import LLM
from .model import (
    ModelError,
    find_loaded,
    load as load_model,
    sync_marker,
    unload as unload_model,
)
from .notify import notify
from . import opencode
from .opencode import OpenCodeError
from .router import Result, Router, split_wake
from .status import cat_status, system_status
from .stt import STTError, Transcriber
from .tts import Speaker


def log(message: str) -> None:
    """Append a local breadcrumb; never let logging break a command."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with (STATE_DIR / "jarvis.log").open("a") as fh:
            fh.write(f"{stamp} {message}\n")
    except OSError:
        pass


def build_ha(config: Config) -> HA:
    return HA(
        url=str(config.get("ha.url", "http://127.0.0.1:8123")),
        token_command=list(config.get("ha.token_command", []) or []) or None,
    )


def type_text(text: str) -> None:
    if not text.strip():
        return
    if shutil.which("wtype"):
        try:
            subprocess.run(
                ["wtype", "-"],
                input=text,
                text=True,
                check=True,
                timeout=10,
            )
            return
        except Exception:  # noqa: BLE001 - fall back to the clipboard
            pass
    if shutil.which("wl-copy"):
        subprocess.run(["wl-copy"], input=text, text=True, check=False)
        notify("Voice Assistant", "Copied to clipboard.")
    else:
        notify("Voice Assistant", "Could not type the transcript.")


def resolve_result(config: Config, result: Result, ha: HA | None = None) -> Result:
    if result.kind == "status":
        return Result("say", system_status(config))
    if result.kind == "cat_status":
        return Result("say", cat_status(config, ha or build_ha(config)))
    if result.kind == "opencode_start":
        return Result(
            "say",
            opencode.start(config, result.text, result.directory),
        )
    if result.kind == "opencode_status":
        return Result("say", opencode.status(config, result.text))
    return result


def route_text(
    config: Config,
    text: str,
    *,
    command: bool = False,
    dry_run: bool = False,
    announce: bool = True,
) -> tuple[Result, HA]:
    """Route and execute text, returning the result and HA client."""
    is_command, remainder = split_wake(text)
    if not command and not is_command:
        return Result("type", text), build_ha(config)

    ha = build_ha(config)
    router = Router(
        config,
        ha=ha,
        llm=LLM(config),
        resolver=JustWatchResolver(config),
    )
    result = resolve_result(config, router.route(remainder or text), ha)

    log(
        f"route kind={result.kind} say={result.text!r} "
        f"actions={len(result.actions)} command={command}"
    )
    Executor(ha=ha, dry_run=dry_run).run(result)
    if result.text:
        if announce:
            Speaker(config, ha).say(result.text)
        else:
            notify("Voice Assistant", result.text)
    return result, ha


def handle_text(
    config: Config,
    text: str,
    *,
    command: bool = False,
    dry_run: bool = False,
) -> int:
    is_command, _ = split_wake(text)
    if not command and not is_command:
        type_text(text)
        return 0
    route_text(config, text, command=command, dry_run=dry_run)
    return 0


def process_audio(
    config: Config,
    path: Path,
    *,
    command: bool,
    dry_run: bool,
) -> int:
    notify("Voice Assistant", "Transcribing…")
    try:
        text = Transcriber(config).transcribe(path)
    except STTError as exc:
        log(f"stt error: {exc}")
        notify("Voice Assistant", str(exc))
        return 1
    if not text:
        log("stt: empty transcript")
        notify("Voice Assistant", "I didn't hear anything.")
        return 1
    log(f"transcript={text!r}")
    return handle_text(config, text, command=command, dry_run=dry_run)


def cmd_toggle(config: Config, args) -> int:
    recording = Recording(config)
    if recording.active():
        path = recording.stop()
        log("record stop")
        if path is None:
            notify("Voice Assistant", "No audio captured.")
            return 1
        return process_audio(
            config,
            path,
            command=args.command,
            dry_run=args.dry_run,
        )

    try:
        recording.start(max_seconds=args.max_seconds)
    except AudioError as exc:
        log(f"record start failed: {exc}")
        notify("Voice Assistant", str(exc))
        return 1
    log("record start")
    notify("Voice Assistant", "Listening… press again to stop.")
    return 0


def cmd_listen(config: Config, args) -> int:
    recording = Recording(config)
    try:
        recording.start(max_seconds=args.max_seconds)
    except AudioError as exc:
        notify("Voice Assistant", str(exc))
        return 1
    notify("Voice Assistant", "Listening… press Enter to stop.")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    path = recording.stop()
    if path is None:
        notify("Voice Assistant", "No audio captured.")
        return 1
    return process_audio(
        config,
        path,
        command=args.command,
        dry_run=args.dry_run,
    )


def cmd_record(config: Config, args) -> int:
    recording = Recording(config)

    if args.action == "start":
        if recording.active():
            notify("Voice Assistant", "Already recording.")
            return 0
        try:
            recording.start(max_seconds=args.max_seconds)
        except AudioError as exc:
            log(f"record start failed: {exc}")
            notify("Voice Assistant", str(exc))
            return 1
        log("record start (hold)")
        notify("Voice Assistant", "Listening… release to stop.")
        return 0

    if args.action == "cancel":
        recording.cancel()
        log("record cancel (hold)")
        notify("Voice Assistant", "Cancelled.")
        return 0

    # Stop can arrive a few milliseconds after start; wait briefly for the
    # state file instead of losing a very short hold.
    for _ in range(10):
        if recording.active():
            break
        time.sleep(0.05)
    if not recording.active():
        notify("Voice Assistant", "No recording in progress.")
        return 1

    path = recording.stop()
    log("record stop (hold)")
    if path is None:
        notify("Voice Assistant", "No audio captured.")
        return 1
    return process_audio(config, path, command=args.command, dry_run=args.dry_run)


def cmd_ask(config: Config, args) -> int:
    return handle_text(
        config,
        " ".join(args.text),
        command=True,
        dry_run=args.dry_run,
    )


def cmd_simulate(config: Config, args) -> int:
    router = Router(
        config,
        ha=build_ha(config),
        llm=LLM(config),
        resolver=JustWatchResolver(config),
    )
    result = router.route(" ".join(args.text))
    print(f"kind={result.kind} say={result.text!r}")
    for action in result.actions:
        print(
            "  "
            f"service={action.service!r} "
            f"target={action.target!r} "
            f"data={action.data!r} "
            f"shell={action.shell!r} "
            f"hypr={action.hypr!r}"
        )
    return 0


def cmd_transcribe(config: Config, args) -> int:
    try:
        text = Transcriber(config).transcribe(Path(args.file))
    except STTError as exc:
        print(exc)
        return 1
    print(text)
    return 0


def cmd_process(config: Config, args) -> int:
    """Transcribe an audio file and (unless --dictate) route it as a command."""
    path = Path(args.file)
    try:
        text = Transcriber(config).transcribe(path)
    except STTError as exc:
        log(f"stt error: {exc}")
        payload = {"error": str(exc), "transcript": "", "say": "", "actions": []}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
            return 1
        notify("Voice Assistant", str(exc))
        return 1

    if not text:
        payload = {
            "transcript": "",
            "kind": "say",
            "say": "I didn't hear anything.",
            "actions": [],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
            return 0
        notify("Voice Assistant", payload["say"])
        return 0

    log(f"transcript={text!r}")

    if args.dictate:
        payload = {"transcript": text, "kind": "type", "say": "", "actions": []}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(text)
        return 0

    try:
        result, _ha = route_text(
            config,
            text,
            command=True,
            dry_run=args.dry_run,
            announce=False,
        )
    except Exception as exc:  # noqa: BLE001 - phone clients need JSON on any failure
        payload = {
            "error": str(exc),
            "transcript": text,
            "kind": "error",
            "say": "",
            "actions": [],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
            return 1
        notify("Voice Assistant", str(exc))
        return 1

    payload = {
        "transcript": text,
        "kind": result.kind,
        "say": result.text,
        "actions": [asdict(action) for action in result.actions],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_opencode(config: Config, args) -> int:
    rest = list(args.rest)
    if args.action == "start":
        prompt = " ".join(rest).strip()
        if not prompt:
            raise OpenCodeError("nothing to send to OpenCode")
        print(opencode.start(config, prompt, args.dir))
        return 0
    print(opencode.status(config, rest[0] if rest else ""))
    return 0


def cmd_model(config: Config, args) -> int:
    model = str(config.get("llm.model", "qwen/qwen3.8-27b"))
    action = args.action
    quiet = getattr(args, "quiet", False)

    entry = find_loaded(model)

    if action == "sync":
        changed = sync_marker(entry is not None)
        if not quiet:
            state = "loaded" if entry is not None else "not loaded"
            print(f"{model}: {state}; marker {'updated' if changed else 'unchanged'}")
        return 0

    if action == "status":
        if entry:
            size = int(entry.get("sizeBytes", 0)) / (1024**3)
            context = entry.get("contextLength", "?")
            print(f"{model}: loaded ({size:.1f} GB, context {context})")
        else:
            print(f"{model}: not loaded")
        return 0

    if action == "load" and entry is not None:
        sync_marker(True)
        if not quiet:
            print(f"{model}: already loaded")
        return 0

    if action == "unload" and entry is None:
        sync_marker(False)
        if not quiet:
            print(f"{model}: not loaded")
        return 0

    if action == "unload" or (action == "toggle" and entry):
        unload_model(str((entry or {}).get("identifier") or model))
        log(f"model unloaded: {model}")
        sync_marker(False)
        if not quiet:
            notify("Voice Assistant", f"{model} unloaded.")
        return 0

    if not quiet:
        notify("Voice Assistant", f"Loading {model}… this takes about a minute.")
    load_model(model, config)
    log(f"model loaded: {model}")
    sync_marker(True)
    if not quiet:
        notify("Voice Assistant", f"{model} loaded.")
    return 0


def cmd_stop(config: Config, args) -> int:
    path = Recording(config).stop()
    notify("Voice Assistant", "Stopped." if path else "Nothing was recording.")
    return 0


def cmd_status(config: Config, args) -> int:
    recording = Recording(config)
    print(f"config: {config.path}")
    print(f"recording: {'yes' if recording.active() else 'no'}")
    print(f"stt model: {config.get('stt.model', 'small.en')}")
    print(f"ha: {config.get('ha.url', 'http://127.0.0.1:8123')}")
    print(f"llm: {'enabled' if config.get('llm.enabled', False) else 'disabled'}")
    print(f"wake words: {', '.join(config.get('input.wake_words', ['jarvis']))}")
    return 0


def cmd_serve(config: Config, args) -> int:
    from .server import serve

    return serve(config, host=args.host, port=args.port)


def cmd_doctor(config: Config, args) -> int:
    checks: list[tuple[str, bool]] = []

    for binary in (
        "pw-record",
        "wtype",
        "wl-copy",
        "notify-send",
        "hyprctl",
        "secret-tool",
        "nvidia-smi",
    ):
        checks.append((binary, shutil.which(binary) is not None))

    checks.append((f"config ({config.path})", config.path.exists()))

    try:
        ha = build_ha(config)
        ha.request("GET", "/api/")
        checks.append(("home assistant", True))
    except Exception as exc:  # noqa: BLE001
        checks.append((f"home assistant ({exc})", False))

    checks.append(
        ("faster-whisper", importlib.util.find_spec("faster_whisper") is not None)
    )

    model_root = Path(str(config.get("stt.download_root", "models")))
    if not model_root.is_absolute():
        model_root = config.directory / model_root
    model_files = list(model_root.glob("**/*")) if model_root.exists() else []
    checks.append((f"stt models ({len(model_files)} files)", bool(model_files)))

    llm = LLM(config)
    if llm.enabled:
        models = llm.models()
        checks.append((f"llm server ({llm.base_url}, {len(models)} models)", bool(models)))
    else:
        checks.append(("llm (disabled)", True))

    checks.append(("recording idle", not Recording(config).active()))
    checks.append(
        (
            "hyprdeck config",
            (Path.home() / ".config/hyprdeck/config.toml").exists(),
        )
    )
    bridge = Path(
        str(
            config.get(
                "opencode.bridge",
                "~/.local/share/opencode-lmstudio-bridge/server.mjs",
            )
        )
    ).expanduser()
    checks.append((f"opencode bridge ({bridge})", bridge.is_file()))
    node = config.get("opencode.node") or shutil.which("node")
    checks.append(("node", bool(node) and (not str(node).startswith("/") or Path(str(node)).is_file())))

    for label, ok in checks:
        print(f"{'✓' if ok else '✗'} {label}")
    return 0 if all(ok for _, ok in checks) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="Local push-to-talk voice layer for Omarchy + Home Assistant.",
    )
    parser.add_argument("--config", default=None, help="path to config.toml")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print actions instead of executing them",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    toggle = sub.add_parser("toggle", help="start/stop recording")
    toggle.add_argument(
        "--command",
        action="store_true",
        help="treat the transcript as a command, even without a wake word",
    )
    toggle.add_argument("--max-seconds", type=int, default=None)
    toggle.set_defaults(func=cmd_toggle)

    listen = sub.add_parser("listen", help="record until Enter, then process")
    listen.add_argument("--command", action="store_true")
    listen.add_argument("--max-seconds", type=int, default=None)
    listen.set_defaults(func=cmd_listen)

    process = sub.add_parser(
        "process",
        help="transcribe an audio file and route it as a command",
    )
    process.add_argument("file")
    process.add_argument(
        "--dictate",
        action="store_true",
        help="return the transcript without routing or typing it",
    )
    process.add_argument("--json", action="store_true", help="emit JSON")
    process.set_defaults(func=cmd_process)

    opencode_parser = sub.add_parser(
        "opencode",
        help="delegate work to OpenCode or check its jobs",
    )
    opencode_parser.add_argument("action", choices=["start", "status"])
    opencode_parser.add_argument("rest", nargs="*")
    opencode_parser.add_argument("--dir", default=None, help="project directory for start")
    opencode_parser.set_defaults(func=cmd_opencode)

    record = sub.add_parser("record", help="hold-to-talk start/stop")
    record.add_argument("action", choices=["start", "stop", "cancel"])
    record.add_argument(
        "--command",
        action="store_true",
        help="route the stop transcript as a command",
    )
    record.add_argument("--max-seconds", type=int, default=None)
    record.set_defaults(func=cmd_record)

    ask = sub.add_parser("ask", help="route text without recording")
    ask.add_argument("text", nargs="+")
    ask.set_defaults(func=cmd_ask)

    simulate = sub.add_parser("simulate", help="show the route for text")
    simulate.add_argument("text", nargs="+")
    simulate.set_defaults(func=cmd_simulate)

    transcribe = sub.add_parser("transcribe", help="transcribe an audio file")
    transcribe.add_argument("file")
    transcribe.set_defaults(func=cmd_transcribe)

    model = sub.add_parser("model", help="load, unload, or inspect the local LLM")
    model.add_argument(
        "action",
        nargs="?",
        default="toggle",
        choices=["toggle", "load", "unload", "status", "sync"],
    )
    model.add_argument("--quiet", action="store_true")
    model.set_defaults(func=cmd_model)

    stop = sub.add_parser("stop", help="stop an active recording")
    stop.set_defaults(func=cmd_stop)

    status = sub.add_parser("status", help="show current state")
    status.set_defaults(func=cmd_status)

    serve = sub.add_parser(
        "serve",
        help="run the local HTTP API used by Home Assistant Assist",
    )
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.set_defaults(func=cmd_serve)

    doctor = sub.add_parser("doctor", help="check the local dependencies")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = config_module.load(args.config)
    try:
        return int(args.func(config, args))
    except (AudioError, HAError, STTError, ModelError, OpenCodeError) as exc:
        notify("Voice Assistant", str(exc))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
