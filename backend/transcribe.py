import sys
from pathlib import Path

from engine.config import load
from engine.stt import Transcriber

print(Transcriber(load()).transcribe(Path(sys.argv[1])))
