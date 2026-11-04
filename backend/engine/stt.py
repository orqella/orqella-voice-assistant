from __future__ import annotations

from pathlib import Path

from .config import Config


class STTError(RuntimeError):
    pass


class Transcriber:
    """A lazy faster-whisper wrapper.

    Importing ``faster_whisper`` is deferred so the rest of Voice Assistant (routing,
    HA calls, tests) works before the speech model is installed.
    """

    def __init__(self, config: Config):
        self.config = config
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model

        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # noqa: BLE001
            raise STTError(
                "faster-whisper is not installed. "
                "Open Voice Assistant settings and run the engine setup."
            ) from exc

        model_name = str(self.config.get("stt.model", "small.en"))
        device = str(self.config.get("stt.device", "cpu"))
        compute_type = str(self.config.get("stt.compute_type", "int8"))
        download_root = Path(str(self.config.get("stt.download_root", "models")))
        if not download_root.is_absolute():
            download_root = self.config.directory / download_root
        download_root.mkdir(parents=True, exist_ok=True)

        # A relative model name may be a local CTranslate2 directory (as
        # produced by ct2-transformers-converter) or a faster-whisper size
        # such as "small.en".
        model_path = Path(model_name).expanduser()
        if not model_path.is_absolute():
            model_path = self.config.directory / model_path
        model_arg = str(model_path) if model_path.exists() else model_name

        try:
            self._model = WhisperModel(
                model_arg,
                device=device,
                compute_type=compute_type,
                download_root=str(download_root),
            )
        except Exception as exc:  # noqa: BLE001
            raise STTError(
                f"could not load whisper model {model_name!r}: {exc}"
            ) from exc
        return self._model

    def transcribe(self, path: Path) -> str:
        model = self._load()
        language = str(self.config.get("stt.language", "en") or "") or None
        try:
            segments, _info = model.transcribe(
                str(path),
                language=language,
                vad_filter=bool(self.config.get("stt.vad", True)),
                beam_size=1,
                condition_on_previous_text=False,
                without_timestamps=True,
            )
            return " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as exc:  # noqa: BLE001
            raise STTError(f"transcription failed: {exc}") from exc
