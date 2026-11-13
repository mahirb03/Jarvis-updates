"""Connects this computer's Jarvis to the phone relay (Cloudflare Worker).

Checks in every few seconds, picks up commands sent from the iPhone (the relay decides Mac vs PC),
runs them through the normal Jarvis brain, and posts the answers back. Also keeps memory in sync
between the Mac and the PC.
"""
import base64
import json
import os
import platform
import threading
import time

import httpx

from paths import MEMORY_FILE, load_settings, log

DEVICE = os.getenv("DEVICE_NAME") or ("mac" if platform.system() == "Darwin" else "pc")


def _cfg():
    s = load_settings()
    url = (os.getenv("RELAY_URL") or s.get("relay_url") or "").rstrip("/")
    token = os.getenv("RELAY_TOKEN") or s.get("relay_token") or ""
    return url, token


class Relay:
    def __init__(self, handle_chat, handle_approve, emit, tts=None):
        self.handle_chat, self.handle_approve, self.emit, self.tts = handle_chat, handle_approve, emit, tts
        self.ok = False
        self._last_mem = 0

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            url, token = _cfg()
            if not (url and token):
                time.sleep(10)
                continue
            try:
                with httpx.Client(base_url=url, timeout=20,
                                  headers={"authorization": f"Bearer {token}"}) as h:
                    r = h.post("/api/hello", json={"device": DEVICE})
                    if r.status_code == 401:
                        if self.ok or not hasattr(self, "_warned"):
                            self.emit({"type": "notice", "text": "Phone link: wrong passphrase."})
                            self._warned = True
                        self.ok = False
                        time.sleep(30)
                        continue
                    r.raise_for_status()
                    if not self.ok:
                        self.ok = True
                        self.emit({"type": "notice", "text": f"Phone link connected (this computer is '{DEVICE}')."})
                    for c in r.json().get("commands", []):
                        self._run(h, c)
                    if time.time() - self._last_mem > 120:
                        self._sync_memory(h)
            except Exception as e:
                if self.ok:
                    log(f"relay: {type(e).__name__}: {e}")
                self.ok = False
            time.sleep(3)

    def _run(self, h, c):
        try:
            if c["kind"] == "approve":
                orig = h.get(f"/api/cmd/{c['ref']}").json()
                aid = (orig.get("extra") or {}).get("approval_id")
                res = self.handle_approve(aid, c["text"] == "yes")
            else:
                self.emit({"type": "notice", "text": f"📱 {c['text']}"})
                res = self.handle_chat(c["text"])
        except Exception as e:
            res = {"reply": f"Something went wrong: {e}"}
        extra = {"tier": res.get("tier"), "actions": res.get("actions") or []}
        body = {"id": c["id"], "reply": res.get("reply"), "extra": extra}
        if c["kind"] == "approve":
            body["ref"] = c["ref"]
        if res.get("approval"):
            body["approval"] = True
            extra.update(approval_id=res["approval"]["id"], description=res["approval"]["description"])
            if c["kind"] == "approve":  # a second approval in a row: keep it on the original command
                body["ref"] = c["ref"]
        if self.tts and res.get("reply") and not res.get("approval"):
            try:  # the reply in Jarvis's own voice, so the phone sounds the same as the PC
                mp3 = self.tts(res["reply"])
                if mp3:
                    h.post("/api/audio", json={"id": c["ref"] if c["kind"] == "approve" else c["id"],
                                               "b64": base64.b64encode(mp3).decode()})
            except Exception as e:
                log(f"relay: voice for phone failed: {e}")
        h.post("/api/result", json=body)
        if res.get("reply"):
            self.emit({"type": "notice", "text": f"📱 replied: {res['reply'][:200]}"})

    def _sync_memory(self, h):
        """Union of both computers' memories, newest kept, written to both sides."""
        self._last_mem = time.time()
        try:
            local = json.loads(MEMORY_FILE.read_text(encoding="utf-8")) if MEMORY_FILE.exists() else []
        except Exception:
            local = []
        remote = h.get("/api/memory").json().get("items", [])
        merged, seen = [], set()
        for m in sorted(local + remote, key=lambda x: x.get("saved", "")):
            key = m.get("note", "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                merged.append(m)
        if merged != local:
            MEMORY_FILE.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        if merged != remote:
            h.put("/api/memory", json={"items": merged})
