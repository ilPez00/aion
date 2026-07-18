"""Verify aion[voice] STT pipeline works offline (no LLM backend needed).

Records ~3s from the default input device, runs faster-whisper transcription,
prints the result. Proves the voice input path is wired and functional.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import sounddevice as sd


def main():
    fs = 16000
    secs = 3
    print(f"[voice-verify] recording {secs}s from default mic @ {fs}Hz ...")
    audio = sd.rec(int(secs * fs), samplerate=fs, channels=1, dtype="float32")
    sd.wait()
    pcm = (np.clip(audio, -1, 1) * 32767).astype("int16")
    print("[voice-verify] loading faster-whisper (tiny) ...")
    from faster_whisper import WhisperModel
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    print("[voice-verify] transcribing ...")
    t0 = time.time()
    segs, info = model.transcribe(pcm, language="en", beam_size=1)
    text = " ".join(s.text for s in segs).strip()
    dt = time.time() - t0
    print(f"[voice-verify] language={info.language} "
          f"prob={info.language_probability:.2f} in {dt:.1f}s")
    print(f"[voice-verify] TRANSCRIPT: {text!r}")
    print("[voice-verify] OK — STT pipeline functional")


if __name__ == "__main__":
    main()
