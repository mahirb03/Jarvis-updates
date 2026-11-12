"""Pulls new app files from the public update mirror (same idea as B-Side).

The mirror (github.com/mahirb03/Jarvis-updates) holds app/*, plus version.json listing every file and
its sha256. Your data (.env, memory.json, settings.json) lives elsewhere and is never touched.
"""
import hashlib
import json
import os
from pathlib import Path

import httpx

LAUNCHER_VERSION = int(os.getenv("JARVIS_LAUNCHER_VERSION", "1"))
UPDATE_URL = os.getenv("JARVIS_UPDATE_URL",
                       "https://raw.githubusercontent.com/mahirb03/Jarvis-updates/main/")
RELEASES_URL = "https://github.com/mahirb03/Jarvis/releases/latest"


def _local_version(app_dir: Path):
    try:
        return json.loads((app_dir / "version.json").read_text(encoding="utf-8")).get("version")
    except Exception:
        return None


def update_app_files(app_dir: Path, timeout: float = 10) -> dict:
    app_dir = Path(app_dir)
    local = _local_version(app_dir)
    if local in (None, "dev"):
        return {"updated": False, "reason": "This is a development copy; update it with git instead."}
    with httpx.Client(timeout=timeout, follow_redirects=True) as h:
        remote = h.get(UPDATE_URL + "version.json", params={"t": os.urandom(4).hex()}).json()
        if int(remote["version"]) <= int(local):
            return {"updated": False, "version": local, "reason": "Already up to date."}
        if int(remote.get("min_launcher", 1)) > LAUNCHER_VERSION:
            return {"updated": False, "needs_installer": True, "url": RELEASES_URL,
                    "reason": "This update needs the new installer."}
        changed = []
        for f in remote["files"]:
            dest = app_dir / f["path"]
            if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == f["sha256"]:
                continue
            data = h.get(UPDATE_URL + "app/" + f["path"], params={"t": f["sha256"][:8]}).content
            if hashlib.sha256(data).hexdigest() != f["sha256"]:
                raise ValueError(f"checksum mismatch on {f['path']}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".new")
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            changed.append(f["path"])
        (app_dir / "version.json").write_text(json.dumps({"version": remote["version"]}), encoding="utf-8")
    return {"updated": True, "version": remote["version"], "files": changed}
