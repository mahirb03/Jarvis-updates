"""Tools Jarvis can use on your PC, plus the rules for which ones need your approval."""
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from paths import MEMORY_FILE, save_settings
MAX_OUT = 8000

# Commands that only read things / open things run without asking.
SAFE_PREFIXES = ("get-", "test-path", "start-process", "select-string", "measure-object",
                 "resolve-path", "where.exe", "ollama list", "ollama ps", "nvidia-smi")
RISKY_WORDS = ("remove", "delete", "del ", "rm ", "rmdir", "format", "stop-", "kill", "set-",
               "new-", "move", "rename", "copy", "out-file", ">", "invoke-", "iex", "shutdown",
               "restart", "reg ", "install", "uninstall", "winget", "pip ", "npm ", "git push")

TOOLS = [
    {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
    {
        "name": "run_powershell",
        "description": "Run a PowerShell command on the user's Windows PC and return its output. "
                       "Use for checking system info, files, running scripts, git, etc.",
        "input_schema": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The PowerShell command"},
            "cwd": {"type": "string", "description": "Optional working directory"}},
            "required": ["command"]},
    },
    {
        "name": "open_app",
        "description": "Open an app, file, folder, or URL on the PC (e.g. 'spotify', 'notepad', "
                       "'C:\\Users\\mahir\\Documents', 'https://youtube.com').",
        "input_schema": {"type": "object", "properties": {
            "target": {"type": "string"}}, "required": ["target"]},
    },
    {
        "name": "list_dir",
        "description": "List files and folders in a directory.",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "read_file",
        "description": "Read a text file (code, notes, csv, etc.).",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a text file with the given content.",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]},
    },
    {
        "name": "fetch_url",
        "description": "Fetch a web page and return its readable text.",
        "input_schema": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]},
    },
    {
        "name": "send_to_claude",
        "description": "Type a message into the Claude desktop app (whatever chat is open there) and send it, "
                       "so Claude can work on it. Use when the user says things like 'tell Claude...', "
                       "'ask Claude to...', 'get Claude to...'. Write the message as a clear, self-contained "
                       "request in the user's own voice (first person, plain tone), including any details "
                       "Claude will need. Don't mention Jarvis.",
        "input_schema": {"type": "object", "properties": {
            "message": {"type": "string"},
            "send": {"type": "boolean", "description": "Press Enter to send (default true). "
                                                       "False just leaves it typed in for the user to review."}},
            "required": ["message"]},
    },
    {
        "name": "set_voice",
        "description": "Switch the assistant's voice and persona: 'jarvis' (British butler), "
                       "'friday' (Irish), or 'edith' (American).",
        "input_schema": {"type": "object", "properties": {
            "preset": {"type": "string", "enum": ["jarvis", "friday", "edith"]}}, "required": ["preset"]},
    },
    {
        "name": "remember",
        "description": "Save a lasting fact or preference about the user for future conversations.",
        "input_schema": {"type": "object", "properties": {
            "note": {"type": "string"}}, "required": ["note"]},
    },
]


def _cap(text: str) -> str:
    return text if len(text) <= MAX_OUT else text[:MAX_OUT] + f"\n...[cut, {len(text)} chars total]"


def needs_approval(name: str, args: dict) -> bool:
    if os.getenv("AUTO_APPROVE_ALL", "").lower() == "true":
        return False
    if name == "write_file":
        return True
    if name == "run_powershell":
        cmd = args.get("command", "").strip().lower()
        if any(w in cmd for w in RISKY_WORDS):
            return True
        return not cmd.startswith(SAFE_PREFIXES)
    return False


def describe_action(name: str, args: dict) -> str:
    if name == "run_powershell":
        return f"Run this command:\n{args.get('command')}"
    if name == "write_file":
        return f"Write {len(args.get('content', ''))} characters to {args.get('path')}"
    return f"{name}: {json.dumps(args)[:300]}"


def load_memory() -> list:
    try:
        return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def run_tool(name: str, args: dict) -> str:
    try:
        if name == "run_powershell":
            r = subprocess.run(["powershell", "-NoProfile", "-Command", args["command"]],
                               capture_output=True, text=True, timeout=120,
                               cwd=args.get("cwd") or None)
            out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
            return _cap(out.strip() or f"(no output, exit code {r.returncode})")

        if name == "open_app":
            target = args["target"].replace("'", "''")
            subprocess.Popen(["powershell", "-NoProfile", "-Command", f"Start-Process '{target}'"])
            return f"Opened {args['target']}"

        if name == "list_dir":
            p = Path(os.path.expandvars(os.path.expanduser(args["path"])))
            items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            return _cap("\n".join(("[dir]  " if i.is_dir() else "       ") + i.name for i in items[:300]))

        if name == "read_file":
            p = Path(os.path.expandvars(os.path.expanduser(args["path"])))
            return _cap(p.read_text(encoding="utf-8", errors="replace"))

        if name == "write_file":
            p = Path(os.path.expandvars(os.path.expanduser(args["path"])))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
            return f"Wrote {p}"

        if name == "fetch_url":
            r = httpx.get(args["url"], timeout=20, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 Jarvis"})
            soup = BeautifulSoup(r.text, "html.parser")
            for t in soup(["script", "style", "nav", "footer", "header"]):
                t.decompose()
            text = re.sub(r"\n\s*\n+", "\n\n", soup.get_text("\n"))
            return _cap(text.strip())

        if name == "send_to_claude":
            return send_to_claude(args["message"], args.get("send", True))

        if name == "set_voice":
            save_settings(voice_preset=args["preset"])
            return f"Voice switched to {args['preset']}. Your next reply uses it."

        if name == "remember":
            mem = load_memory()
            mem.append({"note": args["note"], "saved": datetime.now().isoformat(timespec="minutes")})
            MEMORY_FILE.write_text(json.dumps(mem, indent=2), encoding="utf-8")
            return "Saved."

        return f"Unknown tool {name}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


def send_to_claude(message: str, send: bool = True) -> str:
    """Paste a message into the Claude desktop app's chat box and (optionally) press Enter."""
    import time
    import win32clipboard as cb
    from pywinauto import Application, keyboard, mouse

    try:
        app = Application(backend="uia").connect(path="claude.exe", timeout=3)
    except Exception:
        return "Error: the Claude desktop app isn't open. Open it on the chat you want, then try again."
    win = app.top_window()
    if win.is_minimized():
        win.restore()
    win.set_focus()
    time.sleep(0.6)

    # Put the message on the clipboard (handles long text and any characters)
    cb.OpenClipboard()
    cb.EmptyClipboard()
    cb.SetClipboardText(message, cb.CF_UNICODETEXT)
    cb.CloseClipboard()

    # Click into the message box: it sits at the bottom-centre of the window
    r = win.rectangle()
    offset = int(os.getenv("CLAUDE_INPUT_OFFSET", "110"))
    mouse.click(coords=((r.left + r.right) // 2, r.bottom - offset))
    time.sleep(0.3)
    keyboard.send_keys("^v")
    time.sleep(0.4)
    if send:
        keyboard.send_keys("{ENTER}")
        return f"Sent to Claude: {message[:120]}"
    return "Typed into Claude (not sent yet) so the user can review it."
