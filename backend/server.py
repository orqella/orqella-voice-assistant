"""Voice Assistant sidecar.

The engine source is vendored under backend/engine. User configuration, the
Python runtime for faster-whisper, the model cache, and route state live under
the module's data directory so module code updates do not overwrite them.
"""
import atexit
import base64
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9/3.10: the preflight reports the requirement.
    tomllib = None
from urllib.error import URLError
from urllib.request import Request, urlopen
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = os.environ.get("ORQELLA_MODULE_TOKEN", "")
CONFIG = json.loads(os.environ.get("ORQELLA_MODULE_CONFIG") or "{}")
MODULE_ROOT = Path.cwd()
BACKEND = Path(__file__).resolve().parent
DATA = MODULE_ROOT / "data"
CONFIG_FILE = DATA / "config.toml"
DEFAULT_CONFIG = BACKEND / "config.default.toml"
VENV = DATA / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
MODEL_MARKER = DATA / "model.ready"
STATE_FILE = DATA / "route_state.json"
# Shared with the Home Assistant module so its Jarvis integration can call the
# engine without a second manual setup step. The file is mode 0600. In a real
# daemon checkout the module root is `<data>/modules/<id>`; tests and portable
# runs keep the token inside the module data directory.
SHARED_TOKEN_FILE = (
    MODULE_ROOT.parent.parent / "voice-engine.token"
    if MODULE_ROOT.parent.name == "modules"
    else DATA / "engine.token"
)
MAX_AUDIO = 30 * 1024 * 1024

DATA.mkdir(parents=True, exist_ok=True)
os.environ["VOICE_DATA_ROOT"] = str(DATA)
os.environ.setdefault("XDG_STATE_HOME", str(DATA / "state"))
os.environ["JARVIS_CONFIG"] = str(CONFIG_FILE)
sys.path.insert(0, str(BACKEND))
if not CONFIG_FILE.exists():
    CONFIG_FILE.write_text(DEFAULT_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")

PYTHON_SUPPORTED = sys.version_info[:2] >= (3, 11)
PYTHON_REQUIRED = "Python 3.11 or newer"


def python_error():
    if PYTHON_SUPPORTED:
        return ""
    return (
        f"{PYTHON_REQUIRED} is required for the voice engine; this sidecar is running "
        f"Python {platform.python_version()}. Install Python 3.11+ and restart Orqella."
    )


def require_python():
    if not PYTHON_SUPPORTED:
        raise RuntimeError(python_error())


def catalog_module():
    require_python()
    import catalog  # noqa: PLC0415

    return catalog


SETUP_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()
SETUP = {"status": "idle", "log": ""}
API_LOCK = threading.Lock()
API_PROCESS = None
API_LAST_ERROR = ""
API_LOG = DATA / "voice-api.log"
API_MARKER = DATA / "voice-api.pid"
TABLE_HEADER = re.compile(r"^\s*\[\[?[A-Za-z0-9_.-]+\]\]?\s*(?:#.*)?$")


def setup_log(line):
    with SETUP_LOCK:
        SETUP["log"] = (SETUP["log"] + str(line) + "\n")[-20000:]


def setup_ready():
    return PYTHON_SUPPORTED and VENV_PY.is_file() and MODEL_MARKER.is_file()


def engine_token():
    """Return the shared engine token, creating it once with 0600 mode."""
    try:
        value = SHARED_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    value = secrets.token_urlsafe(32)
    SHARED_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".voice-token-", dir=SHARED_TOKEN_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, SHARED_TOKEN_FILE)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    return value


def api_settings():
    if tomllib is None:
        return "127.0.0.1", 8790, engine_token()
    try:
        data = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    api = data.get("api") if isinstance(data.get("api"), dict) else {}
    token = str(api.get("token") or "").strip() or engine_token()
    return (
        str(api.get("host") or "127.0.0.1"),
        int(api.get("port") or 8790),
        token,
    )


def api_health(host, port, token):
    health_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    request = Request(f"http://{health_host}:{port}/health")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urlopen(request, timeout=1.5) as response:
            payload = json.loads(response.read() or b"{}")
            return bool(payload.get("ok"))
    except (OSError, ValueError, URLError):
        return False


def api_status():
    host, port, token = api_settings()
    with API_LOCK:
        process = API_PROCESS
        if process is not None and process.poll() is not None:
            process = None
    if process is not None:
        return {
            "ok": True,
            "state": "running",
            "managed": True,
            "pid": process.pid,
            "host": host,
            "port": port,
            "ready": setup_ready(),
            "error": "",
            "log": str(API_LOG),
        }
    if api_health(host, port, token):
        return {
            "ok": True,
            "state": "external",
            "managed": False,
            "pid": 0,
            "host": host,
            "port": port,
            "ready": setup_ready(),
            "error": "",
            "log": str(API_LOG),
        }
    return {
        "ok": True,
        "state": "stopped",
        "managed": False,
        "pid": 0,
        "host": host,
        "port": port,
        "ready": setup_ready(),
        "error": API_LAST_ERROR,
        "log": str(API_LOG),
    }


def _pid_is_voice_api(pid):
    if not pid or pid == os.getpid():
        return False
    if os.name == "nt":
        return False
    try:
        if os.path.isdir("/proc"):
            raw = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
            text = raw.decode("utf-8", "replace")
        else:
            text = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return ("engine.cli" in text and "serve" in text) or "voice_api.py" in text


def cleanup_stale_api():
    try:
        pid = int(API_MARKER.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        API_MARKER.unlink(missing_ok=True)
        return
    if _pid_is_voice_api(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.8)
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    API_MARKER.unlink(missing_ok=True)


def start_api():
    global API_PROCESS, API_LAST_ERROR
    host, port, token = api_settings()
    with API_LOCK:
        process = API_PROCESS
        running = process is not None and process.poll() is None
    if running:
        return api_status()
    cleanup_stale_api()
    if api_health(host, port, token):
        return api_status()
    if not setup_ready():
        API_LAST_ERROR = "Complete the Voice Assistant setup before starting the API."
        return api_status()
    API_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = API_LOG.open("w", encoding="utf-8")
    env = os.environ.copy()
    env["JARVIS_CONFIG"] = str(CONFIG_FILE)
    env["JARVIS_API_TOKEN"] = token
    env["VOICE_DATA_ROOT"] = str(DATA)
    env["PYTHONPATH"] = str(BACKEND)
    env["PYTHONUNBUFFERED"] = "1"
    env["VOICE_API_PARENT_PID"] = str(os.getpid())
    command = [str(VENV_PY), str(BACKEND / "voice_api.py")]
    try:
        process = subprocess.Popen(
            command,
            cwd=str(BACKEND),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=(os.name != "nt"),
        )
    except OSError as error:
        log.close()
        API_LAST_ERROR = str(error)
        return api_status()
    with API_LOCK:
        API_PROCESS = process
    API_MARKER.write_text(str(process.pid) + "\n", encoding="utf-8")
    API_LAST_ERROR = ""
    deadline = time.time() + 8
    while time.time() < deadline:
        if api_health(host, port, token):
            return api_status()
        if process.poll() is not None:
            API_LAST_ERROR = f"Voice API exited with status {process.returncode}; see {API_LOG}."
            with API_LOCK:
                API_PROCESS = None
            API_MARKER.unlink(missing_ok=True)
            return api_status()
        time.sleep(0.25)
    API_LAST_ERROR = f"Voice API did not become healthy at http://{host}:{port}."
    stop_api()
    return api_status()

def stop_api():
    global API_PROCESS
    with API_LOCK:
        process = API_PROCESS
        API_PROCESS = None
    if process is not None:
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                if os.name != "nt":
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            except OSError:
                pass
    API_MARKER.unlink(missing_ok=True)


atexit.register(stop_api)


def setup_status():
    with SETUP_LOCK:
        ready = setup_ready()
        if ready and SETUP["status"] not in {"running", "ready"}:
            SETUP["status"] = "ready"
        return {
            "ready": ready,
            "python_ok": PYTHON_SUPPORTED,
            "python": platform.python_version(),
            "python_required": PYTHON_REQUIRED,
            "error": python_error(),
            "venv": VENV_PY.is_file(),
            "model": MODEL_MARKER.is_file(),
            "job": SETUP["status"],
            "log": SETUP["log"],
            "data_root": str(DATA),
            "config": str(CONFIG_FILE),
        }


def _setup_command(command, *, timeout, env=None, cwd=None):
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=cwd,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if detail:
            setup_log(detail[-4000:])
        raise RuntimeError(
            f"Command exited {proc.returncode}: {' '.join(str(part) for part in command)}"
        )
    return proc


def _configure_windows_runtime():
    if os.name != "nt":
        return
    site_packages = VENV / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    (site_packages / "orqella_voice_runtime.pth").write_text(
        "import os, sys; "
        "[os.add_dll_directory(str(p)) for p in "
        "{sys.prefix, os.path.dirname(sys.executable)} "
        "if os.path.isdir(str(p))]\n",
        encoding="utf-8",
    )


def run_setup():
    with SETUP_LOCK:
        if SETUP["status"] == "running":
            return
        SETUP["status"] = "running"
        SETUP["log"] = ""
    if not PYTHON_SUPPORTED:
        with SETUP_LOCK:
            SETUP["status"] = "failed"
        setup_log(python_error())
        return
    try:
        setup_log("Preparing Python environment…")
        if not VENV_PY.is_file():
            _setup_command(
                [sys.executable, "-m", "venv", str(VENV)],
                timeout=180,
                cwd=str(MODULE_ROOT),
            )
        setup_log("Installing speech dependencies (this can take a few minutes)…")
        packages = [
            "faster-whisper>=1.1.0",
            "av>=14.2,<15",
            "sounddevice>=0.5",
            "psutil>=5.9",
        ]
        if os.name == "nt":
            packages.append("msvc-runtime")
        _setup_command(
            [
                str(VENV_PY),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--only-binary=av",
                *packages,
            ],
            timeout=1800,
            cwd=str(MODULE_ROOT),
        )
        _configure_windows_runtime()
        setup_log("Downloading the speech model…")
        env = os.environ.copy()
        env["JARVIS_CONFIG"] = str(CONFIG_FILE)
        _setup_command(
            [str(VENV_PY), str(BACKEND / "download_model.py"), str(CONFIG_FILE)],
            timeout=3600,
            env=env,
            cwd=str(MODULE_ROOT),
        )
        MODEL_MARKER.write_text("ready\n", encoding="utf-8")
        with SETUP_LOCK:
            SETUP["status"] = "ready"
        setup_log("Voice engine ready.")
        start_api()
    except Exception as error:
        with SETUP_LOCK:
            SETUP["status"] = "failed"
        setup_log(f"Setup failed: {error}")
    finally:
        if SETUP["status"] != "ready":
            with SETUP_LOCK:
                if SETUP["status"] == "running":
                    SETUP["status"] = "failed"


def load_disabled():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(str(key) for key in data.get("disabled", []))
    except (OSError, ValueError):
        return set()


def save_disabled(disabled):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="route_state-", suffix=".json", dir=STATE_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"disabled": sorted(disabled)}, indent=2))
        os.replace(temporary, STATE_FILE)
    finally:
        Path(temporary).unlink(missing_ok=True)


def route_key(group_id, item_name):
    slug = re.sub(r"[^a-z0-9]+", "-", str(item_name).lower()).strip("-")
    return f"{group_id}/{slug}"


def display_text(value):
    return str(value or "").replace("Jarvis's", "voice engine's").replace("Jarvis", "voice engine").replace("jarvis", "voice engine")


def _toml_string(raw):
    raw = str(raw or "")
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    try:
        return json.loads(raw)
    except ValueError:
        return raw.strip("\"'")


def _is_table_header(line):
    return bool(TABLE_HEADER.match(line.rstrip("\r\n")))


def _remove_table(text, header):
    lines = text.splitlines(keepends=True)
    pattern = re.compile(r"^\s*\[\s*" + re.escape(header) + r"\s*\]\s*(?:#.*)?$")
    for start, line in enumerate(lines):
        if not pattern.match(line.rstrip("\r\n")):
            continue
        end = start + 1
        while end < len(lines) and not _is_table_header(lines[end]):
            end += 1
        return "".join(lines[:start] + lines[end:])
    raise ValueError(f"route table [{header}] was not found")


def _remove_mode(text, name):
    lines = text.splitlines(keepends=True)
    start = 0
    while start < len(lines):
        if not re.match(r"^\s*\[\[mode\]\]\s*(?:#.*)?$", lines[start].rstrip("\r\n")):
            start += 1
            continue
        end = start + 1
        while end < len(lines) and not _is_table_header(lines[end]):
            end += 1
        block = "".join(lines[start:end])
        match = re.search(r"(?m)^\s*name\s*=\s*(\"(?:[^\"\\]|\\.)*\"|'[^']*')", block)
        if match and _toml_string(match.group(1)).casefold() == str(name).casefold():
            return "".join(lines[:start] + lines[end:])
        start = end
    raise ValueError(f"mode {name!r} was not found")


def _remove_app(text, key):
    lines = text.splitlines(keepends=True)
    start = 0
    while start < len(lines):
        if not re.match(r"^\s*\[apps\]\s*(?:#.*)?$", lines[start].rstrip("\r\n")):
            start += 1
            continue
        end = start + 1
        while end < len(lines) and not _is_table_header(lines[end]):
            end += 1
        for index in range(start + 1, end):
            match = re.match(r"^\s*(\"(?:[^\"\\]|\\.)*\"|'[^']*'|[A-Za-z0-9_-]+)\s*=", lines[index])
            if not match or _toml_string(match.group(1)) != str(key):
                continue
            stop = index + 1
            depth = lines[index].count("[") - lines[index].count("]") + lines[index].count("{") - lines[index].count("}")
            while stop < end and depth > 0:
                depth += lines[stop].count("[") - lines[stop].count("]") + lines[stop].count("{") - lines[stop].count("}")
                stop += 1
            return "".join(lines[:index] + lines[stop:])
        raise ValueError(f"application {key!r} was not found in [apps]")
    raise ValueError("config.toml has no [apps] table")


def _write_config(text):
    backup = CONFIG_FILE.with_name(CONFIG_FILE.name + ".bak")
    shutil.copy2(CONFIG_FILE, backup)
    fd, temporary = tempfile.mkstemp(prefix="config-", suffix=".toml", dir=DATA)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.chmod(temporary, CONFIG_FILE.stat().st_mode)
        os.replace(temporary, CONFIG_FILE)
    finally:
        Path(temporary).unlink(missing_ok=True)


def delete_config_route(target):
    if not isinstance(target, dict):
        raise ValueError("a route deletion target is required")
    kind = str(target.get("type") or "")
    with CONFIG_LOCK:
        text = CONFIG_FILE.read_text(encoding="utf-8")
        if kind == "mode":
            updated = _remove_mode(text, target.get("name"))
        elif kind == "app":
            updated = _remove_app(text, target.get("key"))
        elif kind == "appletv_show":
            updated = _remove_table(text, "appletv.shows." + str(target.get("key")))
        else:
            raise ValueError("this route is managed by the voice engine and cannot be deleted from config.toml")
        _write_config(updated)
        catalog_module().invalidate_cache()
        return {"ok": True, "deleted": target}


def catalog(force=False):
    data = catalog_module().voice_capabilities(force)
    disabled = load_disabled()
    for group in data.get("groups", []):
        group["label"] = display_text(group.get("label"))
        group["description"] = display_text(group.get("description"))
        group["source"] = display_text(group.get("source"))
        for item in group.get("items", []):
            item["name"] = display_text(item.get("name"))
            item["description"] = display_text(item.get("description"))
            item["detail"] = display_text(item.get("detail"))
            item["key"] = route_key(group.get("id"), item.get("name"))
            item["enabled"] = item["key"] not in disabled
    return data


def normal(value):
    return re.sub(r"[^\w\s']", " ", str(value or "").lower()).strip()


def match_route(transcript):
    needle = normal(transcript)
    if not needle:
        return None
    best = None
    for group in catalog(False).get("groups", []):
        for item in group.get("items", []):
            for phrase in item.get("phrases") or []:
                candidate = normal(phrase)
                if candidate and candidate in needle:
                    score = len(candidate)
                    if best is None or score > best[0]:
                        best = (score, group, item)
    if best is None:
        return None
    _, group, item = best
    return {
        "group": group.get("id"),
        "group_label": group.get("label"),
        "name": item.get("name"),
        "key": item.get("key"),
        "enabled": item.get("enabled", True),
    }


def transcribe(path):
    if not setup_ready():
        raise RuntimeError("voice engine setup is not complete")
    env = os.environ.copy()
    env["JARVIS_CONFIG"] = str(CONFIG_FILE)
    result = subprocess.run(
        [str(VENV_PY), str(BACKEND / "transcribe.py"), str(path)],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=str(MODULE_ROOT),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail[-1200:] or "transcription failed")
    return (result.stdout or "").strip()


def execute(text):
    require_python()
    from engine.actions import Executor
    from engine.cli import build_ha, resolve_result
    from engine.config import load
    from engine.deeplinks import JustWatchResolver
    from engine.llm import LLM
    from engine.router import Router

    config = load(str(CONFIG_FILE))
    ha = build_ha(config)
    router = Router(config, ha=ha, llm=LLM(config), resolver=JustWatchResolver(config))
    result = resolve_result(config, router.route(text), ha)
    Executor(ha=ha).run(result)
    return {"kind": result.kind, "say": result.text, "actions": [asdict(action) for action in result.actions]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        supplied = self.headers.get("X-Orqella-Module-Token") or ""
        if TOKEN and hmac.compare_digest(supplied, TOKEN):
            return True
        self.send_json(401, {"error": "unauthorized"})
        return False

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > 48 * 1024 * 1024:
            raise ValueError("request body too large")
        value = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value

    def do_GET(self):
        if not self.authorized():
            return
        try:
            if self.path == "/health":
                return self.send_json(200, {"ok": True})
            if self.path.startswith("/api/status"):
                return self.send_json(200, setup_status())
            if self.path.startswith("/api/routes"):
                force = "refresh=1" in self.path or "refresh=true" in self.path
                return self.send_json(200, catalog(force))
            if self.path.startswith("/api/setup/status"):
                return self.send_json(200, setup_status())
            if self.path == "/api/voice-api":
                return self.send_json(200, api_status())
            return self.send_json(404, {"error": "not found"})
        except Exception as error:
            return self.send_json(502, {"error": str(error)})

    def do_POST(self):
        if not self.authorized():
            return
        try:
            if self.path == "/api/setup/start":
                threading.Thread(target=run_setup, daemon=True).start()
                return self.send_json(200, setup_status())
            if self.path == "/api/voice-api/start":
                return self.send_json(200, start_api())
            if self.path == "/api/voice-api/stop":
                stop_api()
                return self.send_json(200, api_status())
            if self.path == "/api/voice-api/restart":
                stop_api()
                return self.send_json(200, start_api())
            if self.path == "/api/voice":
                require_python()
                payload = self.read_json()
                audio = str(payload.get("audio") or "")
                if audio.startswith("data:"):
                    audio = audio.split(",", 1)[-1]
                data = base64.b64decode(audio, validate=True)
                if not data:
                    raise ValueError("empty audio")
                if len(data) > MAX_AUDIO:
                    raise ValueError("audio too large")
                fd, path = tempfile.mkstemp(prefix="voice-", suffix=".webm")
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                    transcript = transcribe(Path(path))
                finally:
                    Path(path).unlink(missing_ok=True)
                if not transcript:
                    return self.send_json(200, {"transcript": "", "kind": "say", "say": "I didn't hear anything.", "actions": []})
                route = match_route(transcript)
                if route and not route.get("enabled", True):
                    return self.send_json(200, {"transcript": transcript, "kind": "say", "say": f"{route['name']} is disabled.", "actions": [], "route": route})
                result = execute(transcript)
                result["transcript"] = transcript
                if route:
                    result["route"] = route
                return self.send_json(200, result)
            if self.path == "/api/routes/delete":
                payload = self.read_json()
                result = delete_config_route(payload.get("route"))
                key = str(payload.get("key") or "")
                if key:
                    disabled = load_disabled()
                    if key in disabled:
                        disabled.discard(key)
                        save_disabled(disabled)
                return self.send_json(200, result)
            if self.path == "/api/toggle":
                payload = self.read_json()
                keys = payload.get("keys")
                if keys is None:
                    keys = [payload.get("key")]
                keys = [str(key) for key in keys if key]
                enabled = bool(payload.get("enabled", True))
                disabled = load_disabled()
                for key in keys:
                    if enabled:
                        disabled.discard(key)
                    else:
                        disabled.add(key)
                save_disabled(disabled)
                return self.send_json(200, {"disabled": sorted(disabled)})
            return self.send_json(404, {"error": "not found"})
        except ValueError as error:
            return self.send_json(400, {"error": str(error)})
        except Exception as error:
            return self.send_json(502, {"error": str(error)})


if __name__ == "__main__":
    cleanup_stale_api()
    if setup_ready():
        start_api()
    port = int(os.environ["ORQELLA_MODULE_PORT"])
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
