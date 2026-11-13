"""AC control through Tuya (Smart Life IR blaster). Same approach as B-Side.

Credentials: TUYA_ACCESS_ID / TUYA_ACCESS_SECRET / TUYA_REGION in .env, or - if those aren't set -
the "tuya" block from B-Side's own config.json on this computer, so nothing needs setting up twice.
"""
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

import httpx

from paths import log

HOSTS = {"in": "https://openapi.tuyain.com", "eu": "https://openapi.tuyaeu.com",
         "us": "https://openapi.tuyaus.com", "cn": "https://openapi.tuyacn.com"}
MODES = ["cool", "heat", "auto", "fan", "dry"]
FANS = ["auto", "low", "mid", "high"]
_T = {"token": "", "exp": 0, "ir": "", "remote": "", "name": ""}
_LOCK = threading.Lock()


def _hmac256(key: bytes, msg: bytes) -> str:
    """HMAC-SHA256 by hand (keeps the exe free of extra stdlib modules)."""
    if len(key) > 64:
        key = hashlib.sha256(key).digest()
    key = key.ljust(64, b"\0")
    inner = hashlib.sha256(bytes(k ^ 0x36 for k in key) + msg).digest()
    return hashlib.sha256(bytes(k ^ 0x5C for k in key) + inner).hexdigest()


class TuyaError(RuntimeError):
    pass


def _creds() -> dict:
    if os.getenv("TUYA_ACCESS_ID") and os.getenv("TUYA_ACCESS_SECRET"):
        return {"access_id": os.getenv("TUYA_ACCESS_ID"), "access_secret": os.getenv("TUYA_ACCESS_SECRET"),
                "region": os.getenv("TUYA_REGION", "in")}
    if os.name == "nt":
        p = Path(os.environ.get("APPDATA", Path.home())) / "B-Side" / "config.json"
    else:
        p = Path.home() / "Library" / "Application Support" / "B-Side" / "config.json"
    try:
        t = json.loads(p.read_text(encoding="utf-8")).get("tuya") or {}
        if t.get("access_id") and t.get("access_secret"):
            return t
    except Exception:
        pass
    raise TuyaError("The AC isn't set up: I couldn't find Tuya keys in .env or in B-Side's settings on this computer.")


def _req(method, path, body=None, token=True):
    c = _creds()
    cid, secret = c["access_id"], c["access_secret"]
    host = HOSTS.get(c.get("region", "in"), HOSTS["in"])
    raw = json.dumps(body).encode() if body is not None else b""
    tok = _token() if token else ""
    ts, nonce = str(int(time.time() * 1000)), os.urandom(16).hex()
    to_sign = "\n".join([method, hashlib.sha256(raw).hexdigest(), "", path])
    sign = _hmac256(secret.encode(), (cid + tok + ts + nonce + to_sign).encode()).upper()
    headers = {"client_id": cid, "sign": sign, "t": ts, "nonce": nonce, "sign_method": "HMAC-SHA256",
               "Content-Type": "application/json"}
    if tok:
        headers["access_token"] = tok
    r = httpx.request(method, host + path, content=raw if body is not None else None, headers=headers, timeout=20)
    j = r.json()
    if not j.get("success"):
        code, msg = j.get("code"), j.get("msg", "")
        if code in (1010, 1011):
            _T["exp"] = 0
        if code == 28841002 or "expire" in msg.lower():
            raise TuyaError("The Tuya cloud trial has expired. Renew it on iot.tuya.com.")
        raise TuyaError(f"Tuya: {msg} ({code})")
    return j.get("result")


def _token():
    if _T["token"] and time.time() < _T["exp"]:
        return _T["token"]
    r = _req("GET", "/v1.0/token?grant_type=1", token=False)
    _T["token"], _T["exp"] = r["access_token"], time.time() + int(r.get("expire_time", 3600)) - 120
    return _T["token"]


def _find_ac():
    if _T["remote"]:
        return
    try:
        r = _req("GET", "/v2.0/cloud/thing/device?page_size=20")
        devs = r if isinstance(r, list) else (r or {}).get("list", [])
    except TuyaError:
        devs = (_req("GET", "/v1.0/iot-01/associated-users/devices?size=50") or {}).get("devices", [])
    hubs = [d for d in devs if d.get("category") == "wnykq"]
    for d in devs:
        nm = (d.get("name") or "").lower()
        if d.get("category") == "infrared_ac" or (d.get("category") == "qt" and ("air" in nm or re.search(r"\bac\b", nm))):
            hub = d.get("gateway_id") or d.get("parent_id") or (hubs[0]["id"] if hubs else "")
            if hub:
                _T.update(ir=hub, remote=d["id"], name=d.get("name") or "AC")
                return
    fallback = None
    for d in devs:
        try:
            remotes = _req("GET", f"/v2.0/infrareds/{d['id']}/remotes") or []
            if isinstance(remotes, dict):
                remotes = remotes.get("list") or remotes.get("remotes") or []
        except Exception:
            continue
        for rm in remotes:
            cand = (d["id"], rm.get("remote_id"), rm.get("remote_name") or "AC")
            if str(rm.get("category_id")) == "5" or any(w in cand[2].lower() for w in ("ac", "air")):
                _T.update(ir=cand[0], remote=cand[1], name=cand[2])
                return
            fallback = fallback or cand
    if not fallback:
        raise TuyaError("I couldn't find an AC on your IR blaster.")
    _T.update(ir=fallback[0], remote=fallback[1], name=fallback[2])


def _try(method, variants):
    """Tuya's IR API differs by version/region: try each route, remember the one that works."""
    order = variants
    if _T.get(method) is not None:
        order = [variants[_T[method]]] + [v for i, v in enumerate(variants) if i != _T[method]]
    last = None
    for path, body in order:
        try:
            r = _req(method, path, body)
            _T[method] = variants.index((path, body))
            return r
        except TuyaError as e:
            last = e
            if "(1108)" not in str(e) and "(1109)" not in str(e):
                raise
    raise last


def _status() -> dict:
    ir, rm = _T["ir"], _T["remote"]
    try:
        r = _try("GET", [(f"/v2.0/infrareds/{ir}/remotes/{rm}/ac/status", None),
                         (f"/v1.0/infrareds/{ir}/remotes/{rm}/ac/status", None),
                         (f"/v2.0/infrareds/{ir}/air-conditioners/{rm}/status", None),
                         (f"/v1.0/infrareds/{ir}/air-conditioners/{rm}/status", None)]) or {}

        def n(k, d):
            try:
                return int(r.get(k, d))
            except Exception:
                return d
        return {"power": n("power", 0) == 1, "temp": n("temp", 24), "mode": MODES[n("mode", 0) % 5],
                "fan": FANS[n("wind", 0) % 4]}
    except TuyaError:
        st = {x["code"]: x.get("value") for x in (_req("GET", f"/v1.0/iot-03/devices/{rm}/status") or [])}
        pw = next((st[k] for k in ("switch", "power", "switch_1") if k in st), False)
        tp = next((st[k] for k in ("temp", "temp_set", "T") if k in st), 24)
        return {"power": pw in (True, 1, "1", "on", "true"), "temp": int(tp or 24), "mode": "cool", "fan": "auto"}


def ac(power: str | None = None, temp: int | None = None, mode: str | None = None,
       fan: str | None = None) -> str:
    """power: on/off. temp: 16-30 (or +1/-1 style via 'warmer'/'cooler' handled by the model). mode/fan by name."""
    with _LOCK:
        _find_ac()
        st = _status()
        if power is None and temp is None and mode is None and fan is None:
            return (f"{_T['name']} is {'on' if st['power'] else 'off'}"
                    + (f", {st['temp']}°C, {st['mode']}, fan {st['fan']}." if st["power"] else "."))
        p = st["power"] if power is None else str(power).lower() in ("on", "true", "1")
        if temp is not None or mode is not None or fan is not None:
            p = True if power is None else p
        t = max(16, min(30, int(temp))) if temp is not None else st["temp"]
        m = mode.lower() if mode and mode.lower() in MODES else st["mode"]
        f = fan.lower() if fan and fan.lower() in FANS else st["fan"]
        ir, rm = _T["ir"], _T["remote"]
        scene = {"power": 1 if p else 0, "mode": MODES.index(m), "temp": t, "wind": FANS.index(f)}
        try:
            _try("POST", [(f"/v2.0/infrareds/{ir}/air-conditioners/{rm}/scenes", scene),
                          (f"/v1.0/infrareds/{ir}/air-conditioners/{rm}/scenes", scene)])
        except TuyaError as e:
            log(f"ac: scene route failed ({e}); trying device commands")
            codes = {x.get("code"): x for x in
                     (_req("GET", f"/v1.0/iot-03/devices/{rm}/functions") or {}).get("functions", [])}
            cmds = []
            if "PowerOn" in codes:
                k = "PowerOn" if p else "PowerOff"
                cmds.append({"code": k, "value": k})
            else:
                pk = next((k for k in ("switch", "power", "switch_1") if k in codes), None)
                if pk:
                    boolean = (codes[pk].get("type") or "").lower() == "boolean"
                    cmds.append({"code": pk, "value": p if boolean else int(p)})
            if p:
                tk = next((k for k in ("temp", "temp_set", "T") if k in codes), None)
                if tk and temp is not None:
                    cmds.append({"code": tk, "value": t})
            if not cmds:
                raise
            _req("POST", f"/v1.0/iot-03/devices/{rm}/commands", {"commands": cmds})
        if not p:
            return f"{_T['name']} turned off."
        return f"{_T['name']} on: {t}°C, {m}, fan {f}."
