# Voice Assistant

First-party external Orqella module for local hold-to-talk commands.

- The page and overview card share the trusted shell microphone bridge.
- Settings contains the live route catalog, per-route/group toggles, expandable
  route details (phrases, action, source), the voice engine setup workflow, and
  an agent prompt for adding routes. Config-defined modes, applications, and
  Apple TV shows can be deleted from the Settings list; engine routes can be
  disabled but not deleted from `config.toml`.
- The page tab is hidden by default (`page_default = false`); enable it from the
  module's Settings tab if needed.
- When the module is disabled, its sidecar is stopped and its resources are not
  used.

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
