from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .config import Config, STATE_DIR


class OpenCodeError(RuntimeError):
    pass


def _node(config: Config) -> str:
    configured = str(config.get("opencode.node", "") or "").strip()
    if configured:
        return str(Path(configured).expanduser())
    found = shutil.which("node")
    if found:
        return found
    candidates = [
        Path.home() / ".local/share/mise/installs/node/latest/bin/node",
        Path("/usr/local/bin/node"),
        Path("/usr/bin/node"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise OpenCodeError("node not found; set opencode.node in config.toml")


def _bridge(config: Config) -> Path:
    raw = str(
        config.get(
            "opencode.bridge",
            "~/.local/share/opencode-lmstudio-bridge/server.mjs",
        )
        or ""
    )
    path = Path(raw).expanduser()
    if not path.is_file():
        raise OpenCodeError(f"OpenCode MCP bridge not found: {path}")
    return path


def _default_dir(config: Config) -> str:
    raw = str(config.get("opencode.default_dir", "~/Work") or "~/Work")
    return str(Path(raw).expanduser())


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

    env = os.environ.copy()
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
            text=True,
            bufsize=1,
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
                "clientInfo": {"name": "jarvis", "version": "0.1"},
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
            proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

        deadline = time.time() + timeout
        raw_result = None
        while time.time() < deadline:
            remaining = max(0.0, deadline - time.time())
            try:
                ready, _, _ = select.select([proc.stdout], [], [], remaining)
            except (OSError, ValueError):
                break
            if not ready:
                break
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
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
    if directory:
        target = Path(directory).expanduser()
        if not target.is_absolute():
            target = Path(_default_dir(config)) / target
        target_dir = str(target)
    else:
        target_dir = _default_dir(config)
    if auto_approve is None:
        auto_approve = bool(config.get("opencode.auto_approve", True))
    _log(f"[start] directory={target_dir}\nprompt: {prompt}")
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
