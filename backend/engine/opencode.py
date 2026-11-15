from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import Config, STATE_DIR

HOST_HOME = Path(os.environ.get("ORQELLA_HOST_HOME") or Path.home()).expanduser()


class OpenCodeError(RuntimeError):
    pass


def host_path(value) -> Path:
    raw = str(value or "").strip()
    if raw == "~":
        return HOST_HOME
    if raw.startswith("~/"):
        return HOST_HOME / raw[2:]
    return Path(raw).expanduser()


def _node(config: Config) -> str:
    configured = str(config.get("opencode.node", "") or "").strip()
    if configured:
        return str(host_path(configured))
    found = shutil.which("node")
    if found:
        return found
    candidates = [
        HOST_HOME / ".local/share/mise/installs/node/latest/bin/node",
        Path("/usr/local/bin/node"),
        Path("/usr/bin/node"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise OpenCodeError("node not found; set opencode.node in config.toml")


def _bridge(config: Config) -> Path:
    raw = str(config.get("opencode.bridge", "") or "").strip()
    if not raw:
        legacy = HOST_HOME / ".local/share/opencode-lmstudio-bridge/server.mjs"
        if legacy.is_file():
            raw = str(legacy)
        else:
            raise OpenCodeError(
                "OpenCode routes need an MCP bridge; set opencode.bridge in config.toml"
            )
    path = host_path(raw)
    if not path.is_file():
        raise OpenCodeError(f"OpenCode MCP bridge not found: {path}")
    return path


def _default_dir(config: Config) -> str:
    raw = str(config.get("opencode.default_dir", "~") or "~")
    return str(host_path(raw))


def _resolve_directory(config: Config, directory: str | None) -> str:
    if not directory:
        return _default_dir(config)
    target = host_path(directory)
    if not target.is_absolute():
        target = Path(_default_dir(config)) / target
    resolved = target.resolve()
    allowed = {HOST_HOME.resolve(), Path(_default_dir(config)).resolve()}
    if not any(resolved.is_relative_to(root) for root in allowed):
        raise OpenCodeError(
            "OpenCode directory must be inside the host home or opencode.default_dir"
        )
    return str(resolved)


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "ORQELLA_MODULE_TOKEN",
        "ORQELLA_MODULE_CONFIG",
        "JARVIS_API_TOKEN",
        "HA_TOKEN",
        "VOICE_ASSISTANT_TOKEN",
    ):
        env.pop(key, None)
    env["HOME"] = str(HOST_HOME)
    return env


def _log(text: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with (STATE_DIR / "opencode.log").open("a") as fh:
            fh.write(f"\n=== {stamp} ===\n{text}\n")
    except OSError:
        pass


def _call_tool(
    config: Config,
    tool: str,
    arguments: dict,
    *,
    directory: str | None = None,
    timeout: float | None = None,
) -> str:
    node = _node(config)
    bridge = _bridge(config)
    start_timeout = float(config.get("opencode.start_timeout", 40))
    if timeout is None:
        timeout = start_timeout if tool == "opencode_start" else 20.0

    env = child_env()
    env["OPENCODE_DEFAULT_DIR"] = directory or _default_dir(config)
    model = str(config.get("opencode.model", "") or "").strip()
    if model:
        env["OPENCODE_DEFAULT_MODEL"] = model

    try:
        proc = subprocess.Popen(
            [node, str(bridge)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env=env,
        )
    except Exception as exc:  # noqa: BLE001
        raise OpenCodeError(f"could not start OpenCode MCP bridge: {exc}") from exc

    assert proc.stdin is not None and proc.stdout is not None
    payloads = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "voice-assistant", "version": "0.1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    ]
    try:
        for payload in payloads:
            proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        proc.stdin.flush()

        deadline = time.time() + timeout
        raw_result = None
        lines: queue.Queue[bytes] = queue.Queue()

        def read_lines() -> None:
            try:
                for line in proc.stdout:
                    lines.put(line)
            except (OSError, ValueError):
                pass

        threading.Thread(target=read_lines, daemon=True).start()
        while time.time() < deadline:
            remaining = max(0.0, deadline - time.time())
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if message.get("id") == 2:
                raw_result = message
                break
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except OSError:
                pass

    if raw_result is None:
        raise OpenCodeError(f"OpenCode MCP bridge did not answer `{tool}` in time")
    if "error" in raw_result:
        error = raw_result.get("error") or {}
        raise OpenCodeError(str(error.get("message") or error))
    result = raw_result.get("result") or {}
    content = result.get("content") or []
    text = "\n".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    ).strip()
    if result.get("isError"):
        raise OpenCodeError(text or f"OpenCode tool `{tool}` failed")
    if not text:
        raise OpenCodeError(f"OpenCode tool `{tool}` returned no text")
    if tool == "opencode_start" and not bool(config.get("opencode.log_prompts", False)):
        _log(f"[{tool}] result received (redacted)")
    else:
        _log(f"[{tool}] {text}")
    return text


def start(
    config: Config,
    prompt: str,
    directory: str | None = None,
    *,
    auto_approve: bool | None = None,
) -> str:
    prompt = (prompt or "").strip()
    if not prompt:
        raise OpenCodeError("nothing to send to OpenCode")
    target_dir = _resolve_directory(config, directory)
    if auto_approve is None:
        auto_approve = bool(config.get("opencode.auto_approve", False))
    if bool(config.get("opencode.log_prompts", False)):
        _log(f"[start] directory={target_dir}\nprompt: {prompt}")
    else:
        _log(f"[start] directory={target_dir} (prompt redacted)")
    report = _call_tool(
        config,
        "opencode_start",
        {
            "prompt": prompt,
            "directory": target_dir,
            "auto_approve": auto_approve,
        },
        directory=target_dir,
    )

    if re.search(r"^Job:\s*\S+\s+\(finished\)", report, re.M):
        return report

    job = re.search(r"^Job:\s*(\S+)", report, re.M)
    session = re.search(r"^Session:\s*(\S+)", report, re.M)
    bits = ["OpenCode is on it."]
    if job:
        bits.append(f"Job {job.group(1)}.")
    if session:
        bits.append(f"Session {session.group(1)}.")
    bits.append("Ask me how it's doing to check.")
    return " ".join(bits)


def status(config: Config, job_id: str = "") -> str:
    arguments = {"job_id": job_id.strip()} if job_id.strip() else {}
    return _call_tool(
        config,
        "opencode_status",
        arguments,
        timeout=float(config.get("opencode.status_timeout", 20)),
    )
