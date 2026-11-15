from __future__ import annotations

import json
import hmac
import os
import tempfile
import threading
import wave
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .actions import Executor
from .cli import build_ha, log, resolve_result
from .config import Config
from .deeplinks import JustWatchResolver
from .llm import LLM
from .router import Router
from .stt import STTError, Transcriber

MAX_BODY = 1_048_576
MAX_AUDIO = 20 * 1024 * 1024
MAX_TEXT = 2_000
DEFAULT_PORT = 8790


class VoiceAssistantHTTPServer(ThreadingHTTPServer):
    """Threaded localhost API with the Voice Assistant config attached."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: Config, token: str = ""):
        super().__init__(address, VoiceAssistantRequestHandler)
        self.config = config
        self.token = token
        self.transcriber: Transcriber | None = None
        self.stt_lock = threading.Lock()
        self.resolver = JustWatchResolver(config)


class VoiceAssistantRequestHandler(BaseHTTPRequestHandler):
    server_version = "Voice AssistantAPI/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log(f"api {self.address_string()} {fmt % args}")

    def _send(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = str(getattr(self.server, "token", "") or "")
        if not token:
            return False
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied, f"Bearer {token}")

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin", "").strip()
        if not origin:
            return True
        try:
            host = (urlparse(origin).hostname or "").lower()
        except ValueError:
            return False
        return host in {"127.0.0.1", "localhost", "::1"}

    def _path(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self._path() == "/health":
            if not self._authorized():
                self._send(
                    401,
                    {"error": "unauthorized"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
                return
            self._send(200, {"ok": True, "name": "jarvis", "version": "0.1"})
            return
        self._send(404, {"error": "not found"})

    def _read_body(self, limit: int) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._send(400, {"error": "invalid content length"})
            return None
        if length <= 0 or length > limit:
            self._send(400, {"error": "invalid request size"})
            return None
        return self.rfile.read(length)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self._path()
        if path not in {"/api/conversation", "/api/stt"}:
            self._send(404, {"error": "not found"})
            return
        if not self._origin_allowed():
            self._send(403, {"error": "origin is not allowed"})
            return
        if not self._authorized():
            self._send(
                401,
                {"error": "unauthorized"},
                headers={"WWW-Authenticate": "Bearer"},
            )
            return
        if path == "/api/conversation":
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self._send(415, {"error": "application/json required"})
                return

        if path == "/api/stt":
            self._handle_stt()
            return

        raw = self._read_body(MAX_BODY)
        if raw is None:
            return
        try:
            payload = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"error": "invalid JSON"})
            return
        if not isinstance(payload, dict):
            self._send(400, {"error": "JSON object required"})
            return

        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send(400, {"error": "text is required"})
            return
        text = text.strip()
        if len(text) > MAX_TEXT:
            self._send(400, {"error": "text is too long"})
            return

        conversation_id = payload.get("conversation_id")
        if conversation_id is not None and not isinstance(conversation_id, str):
            conversation_id = None

        try:
            result = _process(
                self.server.config,
                text,
                resolver=getattr(self.server, "resolver", None),
            )
        except Exception as exc:  # noqa: BLE001 - return a spoken failure
            log(f"api error: {type(exc).__name__}: {exc}")
            self._send(
                200,
                {
                    "say": "I couldn't do that.",
                    "kind": "error",
                    "error": str(exc),
                    "conversation_id": conversation_id,
                    "continue_conversation": False,
                    "actions": [],
                },
            )
            return

        self._send(
            200,
            {
                "say": result.text,
                "kind": result.kind,
                "conversation_id": conversation_id,
                "continue_conversation": False,
                "actions": [asdict(action) for action in result.actions],
            },
        )

    def _handle_stt(self) -> None:
        raw = self._read_body(MAX_AUDIO)
        if raw is None:
            return
        try:
            sample_rate = int(self.headers.get("X-Sample-Rate", "16000"))
            channels = int(self.headers.get("X-Channels", "1"))
            bit_rate = int(self.headers.get("X-Bit-Rate", "16"))
        except ValueError:
            self._send(400, {"error": "invalid audio metadata"})
            return
        if not (8000 <= sample_rate <= 48000) or channels not in {1, 2}:
            self._send(400, {"error": "unsupported audio metadata"})
            return
        if bit_rate not in {8, 16, 32}:
            self._send(400, {"error": "unsupported bit rate"})
            return

        try:
            text = _transcribe(
                self.server,
                raw,
                sample_rate=sample_rate,
                channels=channels,
                bit_rate=bit_rate,
            )
        except STTError as exc:
            log(f"api stt error: {exc}")
            self._send(500, {"error": str(exc), "text": ""})
            return
        except Exception as exc:  # noqa: BLE001 - keep HA informed
            log(f"api stt error: {type(exc).__name__}: {exc}")
            self._send(500, {"error": str(exc), "text": ""})
            return

        self._send(200, {"text": text, "kind": "stt"})


def _transcribe(
    server: VoiceAssistantHTTPServer,
    audio: bytes,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    bit_rate: int = 16,
) -> str:
    """Wrap raw PCM in WAV and transcribe it with the cached Whisper model."""
    with server.stt_lock:
        if server.transcriber is None:
            server.transcriber = Transcriber(server.config)
        fd, path = tempfile.mkstemp(prefix="jarvis-stt-", suffix=".wav")
        os.close(fd)
        try:
            if audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
                Path(path).write_bytes(audio)
            else:
                with wave.open(path, "wb") as wav:
                    wav.setnchannels(channels)
                    wav.setsampwidth(bit_rate // 8)
                    wav.setframerate(sample_rate)
                    wav.writeframes(audio)
            return server.transcriber.transcribe(Path(path))
        finally:
            Path(path).unlink(missing_ok=True)


def _process(config: Config, text: str, *, resolver=None):
    """Route text and execute its actions; return the spoken result."""
    ha = build_ha(config)
    router = Router(
        config,
        ha=ha,
        llm=LLM(config),
        resolver=resolver if resolver is not None else JustWatchResolver(config),
    )
    result = resolve_result(config, router.route(text), ha)
    log(f"assist kind={result.kind} say={result.text!r} actions={len(result.actions)}")
    Executor(ha=ha).run(result)
    return result


def serve(
    config: Config,
    *,
    host: str | None = None,
    port: int | None = None,
) -> int:
    """Run the local Home Assistant Assist API."""
    bind = str(host or config.get("api.host", "127.0.0.1"))
    listen_port = int(port or config.get("api.port", DEFAULT_PORT))
    token = str(config.get("api.token", "") or os.environ.get("JARVIS_API_TOKEN", ""))
    if not token:
        raise SystemExit(
            "jarvis api: api.token or JARVIS_API_TOKEN is required"
        )

    server = VoiceAssistantHTTPServer((bind, listen_port), config, token=token)
    print(f"jarvis api listening on http://{bind}:{listen_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
