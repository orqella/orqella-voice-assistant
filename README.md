# Voice Assistant

First-party external Orqella module for local hold-to-talk commands.

- The overview card uses the trusted shell microphone bridge.
- Settings contains the live route catalog, per-route/group toggles, expandable
  route details (phrases, action, source), the voice engine setup workflow, and
  an agent prompt for adding routes. Config-defined modes, applications, and
  Apple TV shows can be deleted from the Settings list; engine routes can be
  disabled but not deleted from `config.toml`.
- The module is overview-only; there is no standalone page tab.
- When the module is disabled, its sidecar is stopped and its resources are not
  used.
- The module now owns the Home Assistant API process. When setup is complete it
  starts `engine.cli serve` from `data/.venv` on the address configured in
  `data/config.toml` (`api.host`, `api.port`, default `127.0.0.1:8790`).
  Settings shows whether the API is managed by the module, an external process,
  or stopped, with Start/Restart/Stop controls. The older standalone
  `jarvis-api.service` is no longer required and should be disabled after the
  module API is running.
- Python 3.11+ is required by the sidecar because the engine uses `tomllib`.
  Older interpreters now produce a clear setup error instead of a crash loop.

## Engine data

Runtime data lives under the installed module's `data/` directory:

```text
data/config.toml       # user routes, apps, modes, HA/LLM settings
data/models/           # Whisper model cache
data/.venv/            # faster-whisper runtime
data/route_state.json  # disabled route keys
```

The voice engine source is vendored under `backend/engine/`. There is no
runtime dependency on an external voice project.

The Settings tab can create the Python environment and download the configured
Whisper model. The default config uses `small.en`; change `stt.model` in
`data/config.toml` to use another model.

The agent prompt edits `data/config.toml`; it should not modify vendored engine
source.
