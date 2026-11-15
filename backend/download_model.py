import os
import sys
from pathlib import Path

from faster_whisper import WhisperModel
from engine.config import HOST_HOME, load

config = load(sys.argv[1])
model_name = str(config.get("stt.model", "small.en"))
download_root = Path(str(config.get("stt.download_root", "models")))
if str(download_root) == "~":
    download_root = HOST_HOME
elif str(download_root).startswith("~/"):
    download_root = HOST_HOME / str(download_root)[2:]
if not download_root.is_absolute():
    download_root = config.directory / download_root
download_root.mkdir(parents=True, exist_ok=True)
model_path = Path(model_name)
if str(model_path) == "~":
    model_path = HOST_HOME
elif str(model_path).startswith("~/"):
    model_path = HOST_HOME / str(model_path)[2:]
if not model_path.is_absolute():
    model_path = config.directory / model_path
model_arg = str(model_path) if model_path.exists() else model_name
if model_path.exists() and (model_path / "model.bin").is_file():
    print("model present")
    raise SystemExit(0)
WhisperModel(
    model_arg,
    device=str(config.get("stt.device", "cpu")),
    compute_type=str(config.get("stt.compute_type", "int8")),
    download_root=str(download_root),
)
print("model ready")
