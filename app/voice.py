"""Local voice: 'Hey Jarvis' wake word, speech-to-text (Whisper, on your PC) and the assistant's voice.

Runs in background threads and talks to the UI through emit({...}) events.
"""
import asyncio
import os
import queue
import re
import threading
import time

import numpy as np

from paths import load_settings, log

SR = 16000
FRAME = 1280  # 80 ms, what the wake-word model expects
OUT_SR = 24000

# Voices that come closest to the films without copying the actors.
PRESETS = {
    "jarvis": {"name": "Jarvis", "voice": "en-GB-RyanNeural", "rate": "+6%", "pitch": "-4Hz",
               "about": "calm, dry British butler"},
    "friday": {"name": "Friday", "voice": "en-IE-EmilyNeural", "rate": "+6%", "pitch": "+0Hz",
               "about": "warm, quick Irish"},
    "edith":  {"name": "Edith", "voice": "en-US-AriaNeural", "rate": "+4%", "pitch": "-2Hz",
               "about": "crisp, precise American"},
}

FOLLOW_UP = os.getenv("FOLLOW_UP", "true").lower() == "true"
FOLLOW_UP_SECONDS = float(os.getenv("FOLLOW_UP_SECONDS", "8"))
WAKE_THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.5"))
VOICE_FX = os.getenv("VOICE_FX", "true").lower() == "true"
END_PHRASES = re.compile(r"^(that'?s all|that'?s it|thanks?,? (jarvis|friday|edith)|thank you,? (jarvis|friday|edith)|"
                         r"stop listening|never ?mind|go to sleep|goodnight)\b", re.I)


def current_preset() -> dict:
    key = load_settings().get("voice_preset") or os.getenv("VOICE_PRESET", "jarvis")
    p = dict(PRESETS.get(key, PRESETS["jarvis"]))
    if os.getenv("TTS_VOICE_OVERRIDE"):  # manual override from .env
        p["voice"] = os.getenv("TTS_VOICE_OVERRIDE")
    p["key"] = key if key in PRESETS else "jarvis"
    return p


def clean_for_speech(text: str) -> str:
    text = re.sub(r"https?://\S+", "the link", text)
    text = re.sub(r"```.*?```", " (code shown on screen) ", text, flags=re.S)
    text = re.sub(r"[*#`_>|\[\]]", "", text)
    return text.strip()[:1200]


def split_sentences(text: str) -> list:
    parts = re.split(r"(?<=[.!?])\s+", text)
    out, buf = [], ""
    for p in parts:  # merge very short bits so the voice doesn't sound choppy
        buf = (buf + " " + p).strip()
        if len(buf) > 25:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def ai_fx(pcm: np.ndarray) -> np.ndarray:
    """Light 'movie AI' treatment: low-cut, a little doubling and a short small-room reverb."""
    x = pcm.astype(np.float32)
    # gentle low-cut (removes boominess): subtract a ~120 Hz moving average
    n = 200
    y = x - np.convolve(x, np.ones(n, dtype=np.float32) / n, mode="same")
    out = y.copy()
    d = int(0.009 * OUT_SR)  # 9 ms doubling for a slightly synthetic sheen
    out[d:] += 0.22 * y[:-d]
    for ms, g in ((23, .20), (37, .15), (53, .10), (71, .07)):  # small room
        k = int(ms / 1000 * OUT_SR)
        out[k:] += g * y[:-k]
    out *= 0.8
    return np.clip(out, -32768, 32767).astype(np.int16)


class Voice:
    def __init__(self, emit):
        self.emit = emit
        self.enabled = True
        self.speaking = False
        self.stop_speaking = False
        self.mode = None          # None (waiting for wake word) | "wake" | "followup" | "ptt"
        self.ptt_down = False
        self.noise = 300.0
        self.ww = None
        self.stt = None
        self.ready = False
        self.in_conversation = False
        self._tts_lock = threading.Lock()

    # ---------- setup ----------
    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _load(self):
        self.emit({"type": "notice", "text": "Loading voice models (first run downloads ~150 MB)..."})
        try:
            import openwakeword
            import openwakeword.utils as owu
            from openwakeword.model import Model
            if hasattr(owu, "download_models"):  # openwakeword 0.6+
                try:
                    owu.download_models(model_names=["hey_jarvis"])
                except TypeError:
                    owu.download_models()
                self.ww = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
            else:  # older versions ship the models
                paths = [p for p in openwakeword.get_pretrained_model_paths() if "jarvis" in p.lower()]
                self.ww = Model(wakeword_model_paths=paths[:1])
        except Exception as e:
            self.ww = None
            self.emit({"type": "notice", "text": f"Wake word unavailable ({type(e).__name__}: {e}). "
                                                 "Hold Space or click the orb to talk."})
        from faster_whisper import WhisperModel
        device = os.getenv("WHISPER_DEVICE", "cpu")
        self.stt = WhisperModel(os.getenv("WHISPER_MODEL", "base.en"), device=device,
                                compute_type="int8" if device == "cpu" else "float16")
        self.ready = True
        hint = 'Say "Hey Jarvis", or hold Space.' if self.ww else "Hold Space or click the orb to talk."
        self.emit({"type": "notice", "text": f"Voice ready. {hint}"})

    # ---------- controls from the UI ----------
    def ptt(self, down: bool):
        if down:
            if self.speaking:
                self.stop_speaking = True
            self.ptt_down = True
            self._begin("ptt")
        else:
            self.ptt_down = False

    def listen_now(self):
        if not self.speaking:
            self._begin("followup")

    def end_conversation(self):
        self.in_conversation = False
        if self.mode == "followup":
            self.mode = None
            self.emit({"type": "state", "state": "idle"})

    def _begin(self, mode):
        self.mode = mode
        self._buf, self._heard_speech = [], False
        self._t0 = self._last_speech = time.time()
        if mode in ("wake", "ptt"):
            self.in_conversation = True
        self.emit({"type": "state", "state": "listening"})

    # ---------- main loop ----------
    def _run(self):
        try:
            import sounddevice as sd
            self._load()
            with sd.InputStream(samplerate=SR, channels=1, dtype="int16", blocksize=FRAME) as stream:
                n = 0
                while True:
                    data, _ = stream.read(FRAME)
                    a = data[:, 0]
                    if self.speaking or not self.enabled:
                        continue
                    rms = float(np.sqrt(np.mean(a.astype(np.float32) ** 2)))
                    if self.mode is None:
                        if rms < self.noise * 3:
                            self.noise = 0.97 * self.noise + 0.03 * max(rms, 50)
                        if self.ww is not None:
                            scores = self.ww.predict(a)
                            if scores and max(scores.values()) > WAKE_THRESHOLD:
                                self.ww.reset()
                                self._begin("wake")
                        continue
                    self._capture(a, rms)
                    n += 1
                    if n % 2 == 0:
                        self.emit({"type": "level", "v": min(1.0, rms / 4000)})
        except Exception as e:
            log(f"voice loop crashed: {type(e).__name__}: {e}")
            self.emit({"type": "notice", "text": f"Voice stopped: {type(e).__name__}: {e}. You can still type."})

    def _capture(self, a, rms):
        now = time.time()
        self._buf.append(a.copy())
        if rms > max(self.noise * 2.5, 450):
            self._heard_speech, self._last_speech = True, now

        done = False
        if self.mode == "ptt":
            done = not self.ptt_down
        else:
            limit = self._followup_limit if self.mode == "followup" else 6.0
            if not self._heard_speech and now - self._t0 > limit:
                done = True
            elif self._heard_speech and now - self._last_speech > 0.9:
                done = True
        if now - self._t0 > 30:
            done = True
        if not done:
            return

        mode, buf, had_speech = self.mode, self._buf, self._heard_speech
        self.mode, self._buf = None, []
        if (mode == "ptt" or had_speech) and len(buf) > 4:
            self.emit({"type": "state", "state": "transcribing"})
            threading.Thread(target=self._transcribe, args=(np.concatenate(buf),), daemon=True).start()
        else:
            if mode == "followup":
                self.in_conversation = False  # nothing said: conversation's over
            self.emit({"type": "state", "state": "idle"})

    _followup_limit = FOLLOW_UP_SECONDS

    def _transcribe(self, audio):
        t0 = time.time()
        try:
            f = audio.astype(np.float32) / 32768.0
            segs, _ = self.stt.transcribe(f, language="en", beam_size=1,
                                          initial_prompt="Jarvis, Mahir, Spotify, PowerShell, Bandra, AP Realty.")
            text = " ".join(s.text for s in segs).strip()
            text = re.sub(r"^(hey|hi|okay|ok)?[\s,]*(jarvis|friday|edith)[\s,.!?]*", "", text, flags=re.I).strip()
            ms = int((time.time() - t0) * 1000)
            log(f"timing  speech-to-text {ms} ms for {len(audio) / SR:.1f}s of audio: {text!r}")
            if text and not re.fullmatch(r"[\W_]*(thank you|thanks for watching|you)?[\W_]*", text, re.I):
                if END_PHRASES.match(text):
                    self.in_conversation = False
                self.emit({"type": "heard", "text": text, "stt_ms": ms})
            else:
                self.emit({"type": "state", "state": "idle"})
        except Exception as e:
            self.emit({"type": "notice", "text": f"Couldn't transcribe: {e}"})
            self.emit({"type": "state", "state": "idle"})

    # ---------- speaking ----------
    def speak(self, text: str):
        text = clean_for_speech(text)
        if text:
            threading.Thread(target=self._speak, args=(text,), daemon=True).start()

    async def _edge_tts(self, text, p):
        import edge_tts
        out = bytearray()
        async for ch in edge_tts.Communicate(text, p["voice"], rate=p["rate"], pitch=p["pitch"]).stream():
            if ch["type"] == "audio":
                out += ch["data"]
        return bytes(out)

    def _synth(self, text, p) -> np.ndarray:
        """Text -> 24 kHz mono int16. Uses ElevenLabs if configured, otherwise Microsoft neural voices."""
        import miniaudio
        key, vid = os.getenv("ELEVENLABS_API_KEY"), os.getenv("ELEVENLABS_VOICE_ID")
        mp3 = None
        if key and vid:
            try:
                import httpx
                r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{vid}?output_format=mp3_44100_128",
                               headers={"xi-api-key": key},
                               json={"text": text, "model_id": os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5")},
                               timeout=30)
                r.raise_for_status()
                mp3 = r.content
            except Exception as e:
                log(f"ElevenLabs failed, falling back: {e}")
        if mp3 is None:
            mp3 = asyncio.run(self._edge_tts(text, p))
        dec = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=OUT_SR)
        pcm = np.frombuffer(dec.samples, dtype=np.int16)
        return ai_fx(pcm) if VOICE_FX and not (key and vid) else pcm

    def _speak(self, text):
        import sounddevice as sd
        with self._tts_lock:
            self.speaking, self.stop_speaking, self.mode = True, False, None
            self.emit({"type": "state", "state": "speaking"})
            p = current_preset()
            sentences = split_sentences(text)
            q: queue.Queue = queue.Queue(maxsize=3)
            t0 = time.time()

            def producer():  # synthesise sentence 2 while sentence 1 is playing
                for s in sentences:
                    if self.stop_speaking:
                        break
                    try:
                        q.put(self._synth(s, p))
                    except Exception as e:
                        q.put(e)
                        return
                q.put(None)

            threading.Thread(target=producer, daemon=True).start()
            interrupted, first = False, True
            try:
                with sd.OutputStream(samplerate=OUT_SR, channels=1, dtype="int16") as out:
                    while True:
                        item = q.get()
                        if item is None:
                            break
                        if isinstance(item, Exception):
                            raise item
                        if first:
                            log(f"timing  first audio after {int((time.time() - t0) * 1000)} ms")
                            first = False
                        for i in range(0, len(item), 1200):
                            if self.stop_speaking:
                                interrupted = True
                                break
                            chunk = item[i:i + 1200]
                            out.write(chunk.reshape(-1, 1))
                            lvl = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2))) / 6000
                            self.emit({"type": "level", "v": min(1.0, lvl)})
                        if interrupted:
                            break
            except Exception as e:
                log(f"voice output failed: {type(e).__name__}: {e}")
                self.emit({"type": "notice", "text": f"Voice output failed ({type(e).__name__}). Check your internet."})
            finally:
                time.sleep(0.25)  # let the speaker tail die out before listening again
                self.speaking = False
                self.emit({"type": "speak_done"})
                if self.mode == "ptt":
                    return
                if FOLLOW_UP and self.in_conversation and not interrupted and self.ready:
                    self._followup_limit = FOLLOW_UP_SECONDS * (1.5 if text.rstrip().endswith("?") else 1)
                    self._begin("followup")
                else:
                    self.emit({"type": "state", "state": "idle"})
