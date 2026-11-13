"""Local voice: 'Hey Jarvis' wake word, speech-to-text (Whisper, on your PC) and the assistant's voice.

Runs in background threads and talks to the UI through emit({...}) events.
"""
import asyncio
import os
import queue
import re
import threading
import time
from collections import deque

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
BARGE_IN = os.getenv("BARGE_IN", "true").lower() == "true"   # talk over Jarvis to cut it off
BARGE_FACTOR = float(os.getenv("BARGE_FACTOR", "2.5"))         # higher = harder to interrupt (use if it cuts itself off)
END_SILENCE = float(os.getenv("END_SILENCE", "0.7"))           # seconds of quiet that mean you've finished
STOP_ONLY = re.compile(r"^[\W_]*(stop|wait|hold on|shut up|be quiet|quiet|enough|cancel|ok(ay)?|alright)[\W_]*$", re.I)
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


class SentenceStream:
    """Collects streamed reply text and hands whole sentences to the voice as soon as they're complete."""
    def __init__(self, voice, st):
        self.v, self.st, self.buf = voice, st, ""

    def push(self, delta: str):
        self.buf += delta
        while True:
            m = next((m for m in re.finditer(r"(?<=[.!?:])\s+|\n+", self.buf) if m.start() >= 20), None)
            if not m:
                return
            piece, self.buf = self.buf[:m.start()], self.buf[m.end():]
            self.v.feed(self.st, piece)

    def flush(self):
        if self.buf.strip():
            self.v.feed(self.st, self.buf)
        self.buf = ""


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
        self._preroll = deque(maxlen=4)     # last ~0.3 s of mic audio, so a barge-in keeps your first word
        self._out_hist = deque(maxlen=30)   # (time, loudness) of what Jarvis is playing
        self._barge_hits = deque(maxlen=5)
        self.echo_gain = 0.5                # how loud Jarvis's own voice comes back through the mic
        self._audio_started = 0.0
        self._streams = []

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
        self.stt = self._load_whisper()
        self.ready = True
        hint = 'Say "Hey Jarvis", or hold Space.' if self.ww else "Hold Space or click the orb to talk."
        self.emit({"type": "notice", "text": f"Voice ready. {hint}"})

    def _load_whisper(self):
        """Download Whisper into Jarvis's own folder as real files (the shared Hugging Face cache uses
        symlinks, which break on Windows without Developer Mode). If a copy is damaged, fetch it fresh."""
        import shutil
        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model
        from paths import DATA_DIR
        name = os.getenv("WHISPER_MODEL", "base.en")
        device = os.getenv("WHISPER_DEVICE", "cpu")
        folder = DATA_DIR / "models" / f"whisper-{name}"

        def load():
            path = download_model(name, output_dir=str(folder))
            return WhisperModel(path, device=device, compute_type="int8" if device == "cpu" else "float16")
        try:
            return load()
        except Exception as e:
            log(f"whisper load failed ({type(e).__name__}: {e}); re-downloading")
            self.emit({"type": "notice", "text": "Speech model was damaged. Downloading a fresh copy..."})
            shutil.rmtree(folder, ignore_errors=True)
            return load()

    # ---------- controls from the UI ----------
    def ptt(self, down: bool):
        if down:
            if self.speaking:
                self.stop_speaking = True
                self._cancel_streams()
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
                    if not self.enabled:
                        continue
                    rms = float(np.sqrt(np.mean(a.astype(np.float32) ** 2)))
                    self._preroll.append(a.copy())
                    if self.mode is not None:  # capturing what you say
                        self._capture(a, rms)
                        n += 1
                        if n % 2 == 0:
                            self.emit({"type": "level", "v": min(1.0, rms / 4000)})
                        continue
                    if self.speaking:  # Jarvis is talking: keep an ear out for you talking over it
                        self._check_barge(a, rms)
                        continue
                    if rms < self.noise * 3:
                        self.noise = 0.97 * self.noise + 0.03 * max(rms, 50)
                    if self.ww is not None:
                        scores = self.ww.predict(a)
                        if scores and max(scores.values()) > WAKE_THRESHOLD:
                            self.ww.reset()
                            self._begin("wake")
        except Exception as e:
            log(f"voice loop crashed: {type(e).__name__}: {e}")
            self.emit({"type": "notice", "text": f"Voice stopped: {type(e).__name__}: {e}. You can still type."})

    def _check_barge(self, a, rms):
        if not BARGE_IN:
            return
        now = time.time()
        if self.ww is not None:  # "Jarvis" always cuts in
            scores = self.ww.predict(a)
            if scores and max(scores.values()) > WAKE_THRESHOLD:
                self.ww.reset()
                return self._barge(wake=True)
        ref = max((r for t, r in self._out_hist if now - t < 0.6), default=0.0)
        if ref > 300:  # learn how much of Jarvis's own voice leaks into the mic
            ratio = rms / ref
            eg = self.echo_gain
            self.echo_gain = 0.85 * eg + 0.15 * ratio if ratio < eg else 0.985 * eg + 0.015 * min(ratio, 3.0)
        if now - self._audio_started < 0.4:  # still calibrating on the first words
            self._barge_hits.clear()
            return
        thresh = max(self.echo_gain * ref * BARGE_FACTOR + self.noise * 2, self.noise * 4, 700)
        self._barge_hits.append(rms > thresh)
        if sum(self._barge_hits) >= 4:  # ~0.3 s of you clearly talking over it
            self._barge()

    def _barge(self, wake=False):
        log(f"barge-in ({'wake word' if wake else 'voice'}), echo gain {self.echo_gain:.2f}")
        self.stop_speaking = True
        self._cancel_streams()
        self._barge_hits.clear()
        self._begin("wake")
        if not wake:  # keep the start of what you said
            self._buf = list(self._preroll)
            self._heard_speech = True

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
            elif self._heard_speech and now - self._last_speech > END_SILENCE:
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
            segs, _ = self.stt.transcribe(f, language="en", beam_size=1, without_timestamps=True,
                                          condition_on_previous_text=False, initial_prompt="Jarvis, Mahir, Spotify, PowerShell, Bandra, AP Realty.")
            text = " ".join(s.text for s in segs).strip()
            text = re.sub(r"^(hey|hi|okay|ok)?[\s,]*(jarvis|friday|edith)[\s,.!?]*", "", text, flags=re.I).strip()
            ms = int((time.time() - t0) * 1000)
            log(f"timing  speech-to-text {ms} ms for {len(audio) / SR:.1f}s of audio: {text!r}")
            if STOP_ONLY.match(text or ""):  # "stop" / "wait" just shuts Jarvis up
                self.emit({"type": "state", "state": "idle"})
            elif text and not re.fullmatch(r"[\W_]*(thank you|thanks for watching|you)?[\W_]*", text, re.I):
                if END_PHRASES.match(text):
                    self.in_conversation = False
                self.emit({"type": "heard", "text": text, "stt_ms": ms})
            else:
                self.emit({"type": "state", "state": "idle"})
        except Exception as e:
            self.emit({"type": "notice", "text": f"Couldn't transcribe: {e}"})
            self.emit({"type": "state", "state": "idle"})

    # ---------- speaking ----------
    # A "stream" is one reply. Text can be fed in sentence by sentence while Claude is still writing,
    # so Jarvis starts talking before the whole answer is ready.
    def speak(self, text: str):
        st = self.begin()
        for part in split_sentences(clean_for_speech(text)):
            self.feed(st, part, clean=False)
        self.end(st)

    def begin(self) -> dict:
        st = {"q": queue.Queue(), "cancel": False, "chars": 0}
        self._streams.append(st)
        threading.Thread(target=self._speak, args=(st,), daemon=True).start()
        return st

    def feed(self, st: dict, text: str, clean=True):
        if st["cancel"]:
            return
        t = clean_for_speech(text) if clean else text.strip()
        if t and st["chars"] < 1500:
            st["chars"] += len(t)
            st["q"].put(t)

    def end(self, st: dict):
        st["q"].put(None)

    def _cancel_streams(self):
        for st in list(self._streams):
            st["cancel"] = True
            st["q"].put(None)

    def mp3(self, text: str) -> bytes:
        """The reply as an mp3 in the current voice, for the phone app."""
        t = clean_for_speech(text)
        return asyncio.run(self._edge_tts(t, current_preset())) if t else b""

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

    def _speak(self, st):
        import sounddevice as sd
        with self._tts_lock:
            try:
                self._play(st, sd)
            finally:
                if st in self._streams:
                    self._streams.remove(st)

    def _play(self, st, sd):
        if st["cancel"]:
            return
        self.speaking, self.stop_speaking = True, False
        self._audio_started = time.time() + 999
        self._barge_hits.clear()
        p = current_preset()
        audio: queue.Queue = queue.Queue()
        said = []
        t0 = time.time()

        def producer():  # synthesise the next sentence while the current one plays
            while True:
                t = st["q"].get()
                if t is None or st["cancel"] or self.stop_speaking:
                    break
                said.append(t)
                try:
                    audio.put(self._synth(t, p))
                except Exception as e:
                    audio.put(e)
                    return
            audio.put(None)

        threading.Thread(target=producer, daemon=True).start()
        interrupted, first = False, True
        try:
            with sd.OutputStream(samplerate=OUT_SR, channels=1, dtype="int16") as out:
                while True:
                    item = audio.get()
                    if item is None:
                        break
                    if isinstance(item, Exception):
                        raise item
                    if first:
                        log(f"timing  first audio after {int((time.time() - t0) * 1000)} ms")
                        first = False
                        self._audio_started = time.time()
                        if self.mode is None:
                            self.emit({"type": "state", "state": "speaking"})
                    for i in range(0, len(item), 1200):
                        if self.stop_speaking or st["cancel"]:
                            interrupted = True
                            break
                        chunk = item[i:i + 1200]
                        out.write(chunk.reshape(-1, 1))
                        lvl = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
                        self._out_hist.append((time.time(), lvl))
                        self.emit({"type": "level", "v": min(1.0, lvl / 6000)})
                    if interrupted:
                        break
        except Exception as e:
            log(f"voice output failed: {type(e).__name__}: {e}")
            self.emit({"type": "notice", "text": f"Voice output failed ({type(e).__name__}). Check your internet."})
        finally:
            if not first and not interrupted:
                time.sleep(0.2)  # let the speaker tail die out before listening normally again
            self.speaking = False
            self.emit({"type": "speak_done"})
            if self.mode is not None:  # you cut in or pressed talk: already listening
                self.emit({"type": "state", "state": "listening"})
                return
            text = " ".join(said)
            if FOLLOW_UP and said and self.in_conversation and not interrupted and self.ready:
                self._followup_limit = FOLLOW_UP_SECONDS * (1.5 if text.rstrip().endswith("?") else 1)
                self._begin("followup")
            else:
                self.emit({"type": "state", "state": "idle"})
