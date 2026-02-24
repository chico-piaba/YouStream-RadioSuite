#!/usr/bin/env python3
"""
Backend de áudio com fallback: sounddevice (Windows) ou PyAudio.
No Windows, PyAudio pode travar; sounddevice costuma funcionar melhor.
"""
from __future__ import annotations

import logging
import platform

logger = logging.getLogger(__name__)

_BACKEND = None
_BACKEND_NAME = None
IS_WINDOWS = platform.system() == "Windows"


def _init_backend():
    global _BACKEND, _BACKEND_NAME

    if _BACKEND is not None:
        return _BACKEND

    # No Windows: tenta sounddevice primeiro (PyAudio costuma travar)
    if IS_WINDOWS:
        try:
            import sounddevice as sd
            _BACKEND = _SoundDeviceBackend(sd)
            _BACKEND_NAME = "sounddevice"
            logger.info("Backend de áudio: sounddevice")
            return _BACKEND
        except Exception as e:
            logger.warning(f"sounddevice não disponível: {e}")

    # PyAudio (padrão no macOS/Linux, fallback no Windows)
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        _BACKEND = _PyAudioBackend(pa)
        _BACKEND_NAME = "pyaudio"
        logger.info("Backend de áudio: PyAudio")
        return _BACKEND
    except Exception as e:
        logger.warning(f"PyAudio não disponível: {e}")

    # Fallback: sounddevice (se não tentamos antes)
    if not IS_WINDOWS:
        try:
            import sounddevice as sd
            _BACKEND = _SoundDeviceBackend(sd)
            _BACKEND_NAME = "sounddevice"
            logger.info("Backend de áudio: sounddevice")
            return _BACKEND
        except Exception as e:
            logger.warning(f"sounddevice não disponível: {e}")

    raise RuntimeError(
        "Nenhum backend de áudio disponível.\n"
        "Instale: pip install pyaudio\n"
        "Ou no Windows: pip install sounddevice"
    )


def get_backend():
    if _BACKEND is None:
        _init_backend()
    return _BACKEND


def get_backend_name():
    if _BACKEND_NAME is None:
        _init_backend()
    return _BACKEND_NAME


class _PyAudioBackend:
    def __init__(self, pa):
        self._pa = pa

    def get_devices(self):
        devices = []
        for i in range(self._pa.get_device_count()):
            try:
                info = self._pa.get_device_info_by_index(i)
                devices.append({
                    "index": info["index"],
                    "name": info["name"],
                    "maxInputChannels": info["maxInputChannels"],
                    "maxOutputChannels": info["maxOutputChannels"],
                    "defaultSampleRate": info["defaultSampleRate"],
                })
            except Exception:
                pass
        return devices

    def open_input_stream(self, channels, rate, chunk_size, device_index, callback):
        import pyaudio

        def _pa_cb(in_data, frame_count, time_info, status):
            callback(in_data, frame_count, time_info, status)
            return (None, pyaudio.paContinue)

        fmt = self._pa.get_format_from_width(2)
        stream = self._pa.open(
            format=fmt,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=chunk_size,
            stream_callback=_pa_cb,
        )
        return _PyAudioStream(stream, self._pa)

    def open_output_stream(self, channels, rate, chunk_size):
        fmt = self._pa.get_format_from_width(2)
        stream = self._pa.open(
            format=fmt,
            channels=channels,
            rate=rate,
            output=True,
            frames_per_buffer=chunk_size,
        )
        return _PyAudioStream(stream, self._pa)

    def terminate(self):
        try:
            self._pa.terminate()
        except Exception:
            pass


class _PyAudioStream:
    def __init__(self, stream, pa):
        self._stream = stream
        self._pa = pa

    def is_active(self):
        return self._stream.is_active()

    def stop_stream(self):
        self._stream.stop_stream()

    def close(self):
        self._stream.close()

    def write(self, data):
        self._stream.write(data)


class _SoundDeviceBackend:
    def __init__(self, sd):
        self._sd = sd

    def get_devices(self):
        devices = []
        try:
            all_devs = self._sd.query_devices()
            for i, dev in enumerate(all_devs):
                devices.append({
                    "index": i,
                    "name": dev.get("name", f"Device {i}"),
                    "maxInputChannels": int(dev.get("max_input_channels", 0)),
                    "maxOutputChannels": int(dev.get("max_output_channels", 0)),
                    "defaultSampleRate": float(dev.get("default_samplerate", 44100)),
                })
        except Exception as e:
            logger.warning(f"Erro ao listar dispositivos: {e}")
        return devices

    def open_input_stream(self, channels, rate, chunk_size, device_index, callback):
        def _cb(indata, frames, time_info, status):
            data = indata.tobytes()
            callback(data, frames, time_info, status)

        kwargs = dict(
            channels=channels,
            samplerate=rate,
            blocksize=chunk_size,
            dtype="int16",
            callback=_cb,
        )
        if device_index is not None:
            kwargs["device"] = device_index
        stream = self._sd.InputStream(**kwargs)
        stream.start()
        return _SoundDeviceStream(stream)

    def open_output_stream(self, channels, rate, chunk_size):
        stream = self._sd.OutputStream(
            channels=channels,
            samplerate=rate,
            blocksize=chunk_size,
            dtype="int16",
        )
        stream.start()
        return _SoundDeviceStream(stream)

    def terminate(self):
        pass


class _SoundDeviceStream:
    def __init__(self, stream):
        self._stream = stream

    def is_active(self):
        return self._stream.active

    def stop_stream(self):
        self._stream.stop()

    def close(self):
        self._stream.close()

    def write(self, data):
        import numpy as np
        arr = np.frombuffer(data, dtype=np.int16)
        self._stream.write(arr)
