"""Запись с микрофона через WinMM MCI (Windows, без внешних зависимостей)."""
import ctypes
import tempfile
from pathlib import Path

_ALIAS = "ada_mic"
_WAV = Path(tempfile.gettempdir()) / "ada_voice.wav"


def _mci(command: str) -> str:
    buf = ctypes.create_unicode_buffer(256)
    err = ctypes.windll.winmm.mciSendStringW(command, buf, 256, None)
    if err:
        msg = ctypes.create_unicode_buffer(256)
        ctypes.windll.winmm.mciGetErrorStringW(err, msg, 256)
        raise OSError(msg.value or f"MCI error {err}")
    return buf.value


class Recorder:
    """Запись в WAV 16 кГц моно; start/stop вызывать из одного потока."""

    def __init__(self):
        self.active = False

    def start(self):
        _mci(f"open new type waveaudio alias {_ALIAS}")
        try:
            _mci(f"set {_ALIAS} bitspersample 16 channels 1 samplespersec 16000 "
                 f"bytespersec 32000 alignment 2")
            _mci(f"record {_ALIAS}")
        except OSError:
            _mci(f"close {_ALIAS}")
            raise
        self.active = True

    def stop(self) -> bytes:
        try:
            _mci(f"stop {_ALIAS}")
            _mci(f'save {_ALIAS} "{_WAV}"')
        finally:
            _mci(f"close {_ALIAS}")
            self.active = False
        return _WAV.read_bytes()

    def cancel(self):
        try:
            _mci(f"close {_ALIAS}")
        finally:
            self.active = False
