"""Jarvis launcher (this is what Jarvis.exe runs).

- Installed (exe): app code lives in %APPDATA%\\Jarvis\\app and updates itself from the
  public mirror on every launch; your data (.env, memory, settings, log) sits in %APPDATA%\\Jarvis.
- Development (python launcher.py): runs the code in ./app directly, data in this folder.
"""
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

LAUNCHER_VERSION = 1  # bump when the exe itself changes (new packages); also bump MIN_LAUNCHER in the repo
os.environ["JARVIS_LAUNCHER_VERSION"] = str(LAUNCHER_VERSION)
FROZEN = getattr(sys, "frozen", False)
INSTALLED = FROZEN or os.getenv("JARVIS_INSTALLED") == "1"  # the Mac app runs from source but updates itself

if False:  # never runs: tells PyInstaller which packages the app (loaded from AppData) needs
    import asyncio, hashlib, queue, re, subprocess, json as _j  # noqa
    import anthropic, dotenv, fastapi, fastapi.responses, fastapi.staticfiles, pydantic  # noqa
    import uvicorn, uvicorn.loops.auto, uvicorn.protocols.http.auto  # noqa
    import uvicorn.protocols.websockets.auto, uvicorn.lifespan.on, websockets  # noqa
    import httpx, bs4, numpy, sounddevice, miniaudio, edge_tts, webview  # noqa
    import faster_whisper, openwakeword, openwakeword.model, openwakeword.utils  # noqa
    import pywinauto, win32clipboard  # noqa
ROOT = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parent

if FROZEN:
    DATA = Path(os.environ.get("APPDATA", Path.home())) / "Jarvis"
    APP = DATA / "app"
else:
    DATA = Path(os.getenv("JARVIS_DATA") or ROOT)
    APP = ROOT / "app"
DATA.mkdir(parents=True, exist_ok=True)
os.environ["JARVIS_DATA"] = str(DATA)

# No console when launched from the shortcut: send output to the log so nothing crashes silently
if sys.stdout is None or sys.stderr is None:
    _log = open(DATA / "jarvis.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log


def _ver(p: Path):
    try:
        return int(json.loads((p / "version.json").read_text(encoding="utf-8"))["version"])
    except Exception:
        return -1


if FROZEN:
    # First install, or the installer carries newer code than what's in AppData: copy it over
    bundled = ROOT / "app"
    if _ver(bundled) > _ver(APP):
        shutil.copytree(bundled, APP, dirs_exist_ok=True)

sys.path.insert(0, str(APP))
URL = "http://127.0.0.1:8765"


def server_up() -> bool:
    import httpx
    try:
        return httpx.get(URL + "/api/health", timeout=1).status_code == 200
    except Exception:
        return False


def smoke_test():
    """CI check: every package the app needs imports inside the exe."""
    try:
        import server  # noqa  (pulls in tools, voice, paths, anthropic, fastapi...)
        import faster_whisper, openwakeword.model, sounddevice, miniaudio, edge_tts, webview  # noqa
        import uvicorn.protocols.websockets.auto  # noqa
        if sys.platform == "win32":
            import pywinauto, win32clipboard  # noqa
        print("smoke test ok")
        sys.exit(0)
    except SystemExit:
        raise
    except BaseException as e:
        import traceback
        traceback.print_exc()
        print("smoke test FAILED:", e)
        sys.exit(1)


def main():
    if os.getenv("JARVIS_SMOKE_TEST"):
        smoke_test()
    if "--restarted" in sys.argv:
        time.sleep(1.5)  # let the old copy shut down
    if INSTALLED and os.getenv("JARVIS_AUTO_UPDATE", "true") != "false":
        try:
            import updater
            r = updater.update_app_files(APP, timeout=6)
            print("update check:", r)
        except Exception as e:
            print("update check failed:", e)

    if not server_up():
        def run():
            import uvicorn
            from server import app
            uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
        threading.Thread(target=run, daemon=True).start()
        for _ in range(80):
            if server_up():
                break
            time.sleep(0.25)

    import webview
    icon = ROOT / "assets" / "jarvis.ico"
    webview.create_window("J.A.R.V.I.S.", URL, width=1320, height=840, min_size=(900, 600),
                          background_color="#05070d")
    try:
        webview.start(icon=str(icon) if icon.exists() else None)
    except TypeError:
        webview.start()


if __name__ == "__main__":
    main()
