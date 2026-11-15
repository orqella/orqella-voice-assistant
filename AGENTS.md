# Voice Assistant module

Hold-to-talk voice commands with a vendored local engine. The sidecar owns the
engine process and the Home Assistant API; the daemon only provides the module
backend, microphone bridge, and settings surfaces.

- `backend/server.py` owns setup, the shared engine token, the API process, the
  warm transcription worker, route state, and the sidecar API.
- `backend/engine/` is vendored engine code. Keep it independent of Orqella
  daemon internals.
- `data/` is module-private (0700); `data/config.toml` and the shared token are
  0600. Never write runtime state into the checkout.
- Use `ORQELLA_HOST_HOME` for host paths. `HOME` is the module home; `~` in
  configuration means the real user home.
- Never pass `ORQELLA_MODULE_TOKEN`, `ORQELLA_MODULE_CONFIG`, or unrelated
  tokens to child processes. Use `sanitized_env()`; the OpenCode bridge has its
  own `child_env()`.
- Keep `voice-engine.token` synchronized with the effective `[api] token`, and
  guard creation/update with `TOKEN_LOCK`.
- Keep the transcription worker alive; do not reintroduce a Whisper model load
  per request.
- Treat transcript/audio/prompt content as private. Do not log prompts unless
  `opencode.log_prompts` is enabled.
- Keep `opencode.auto_approve` off by default, and reject agent directories
  outside the host home or `opencode.default_dir`.
- Add tests under `tests/` for routing, state, and safety helpers. Run
  `python3 -m unittest discover -s tests -p 'test_*.py'` and
  `orqella module check .` before publishing.
