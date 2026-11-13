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
        "name": "gmail_search",
        "description": "Search Gmail with Gmail search syntax (e.g. 'from:architect newer_than:7d', 'is:unread', "
                       "'subject:BOQ'). account = 'personal', 'work', or an email; omit for the first account.",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string"}, "account": {"type": "string"},
            "max_results": {"type": "integer"}}, "required": ["query"]},
    },
    {
        "name": "gmail_read",
        "description": "Read one email in full by its message id (from gmail_search).",
        "input_schema": {"type": "object", "properties": {
            "message_id": {"type": "string"}, "account": {"type": "string"}}, "required": ["message_id"]},
    },
    {
        "name": "gmail_draft",
        "description": "Save an email as a Gmail draft (does NOT send). For replies pass reply_to_message_id; "
                       "to/subject can then be left blank. Write in the user's plain, direct tone.",
        "input_schema": {"type": "object", "properties": {
            "to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"},
            "cc": {"type": "string"}, "account": {"type": "string"},
            "reply_to_message_id": {"type": "string"}}, "required": ["body"]},
    },
    {
        "name": "gmail_send",
        "description": "Send an email. ONLY when the user explicitly says to send. Prefer sending a draft by "
                       "draft_id; otherwise give to + subject + body. The user is always asked to approve.",
        "input_schema": {"type": "object", "properties": {
            "draft_id": {"type": "string"}, "to": {"type": "string"}, "subject": {"type": "string"},
            "body": {"type": "string"}, "account": {"type": "string"}}},
    },
    {
        "name": "calendar_list",
        "description": "List calendar events between start and end (ISO date or datetime, local time). "
                       "Omit account to check all connected calendars. Defaults to the next 7 days.",
        "input_schema": {"type": "object", "properties": {
            "start": {"type": "string"}, "end": {"type": "string"}, "account": {"type": "string"},
            "query": {"type": "string"}}},
    },
    {
        "name": "calendar_add",
        "description": "Add a calendar event. start/end as local ISO datetimes (e.g. 2026-11-16T11:00). "
                       "end defaults to 1 hour after start. The user approves before it's added.",
        "input_schema": {"type": "object", "properties": {
            "summary": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"},
            "account": {"type": "string"}, "location": {"type": "string"}, "description": {"type": "string"},
            "attendees": {"type": "string", "description": "comma-separated emails (Google emails them invites)"}},
            "required": ["summary", "start"]},
    },
    {
        "name": "calendar_move",
        "description": "Move an event (event id from calendar_list) to a new start; keeps its length unless end is given.",
        "input_schema": {"type": "object", "properties": {
            "event_id": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"},
            "account": {"type": "string"}}, "required": ["event_id", "start"]},
    },
    {
        "name": "todo_list",
        "description": "Show open Microsoft To Do tasks. Omit list_name for all lists; omit account for all "
                       "accounts ('personal' / 'work' / an email).",
        "input_schema": {"type": "object", "properties": {
            "list_name": {"type": "string"}, "account": {"type": "string"},
            "include_done": {"type": "boolean"}}},
    },
    {
        "name": "todo_add",
        "description": "Add a Microsoft To Do task. due = local date or datetime (2026-11-16). Default list if "
                       "list_name is omitted; default account is the first connected.",
        "input_schema": {"type": "object", "properties": {
            "title": {"type": "string"}, "list_name": {"type": "string"}, "account": {"type": "string"},
            "due": {"type": "string"}, "notes": {"type": "string"}, "important": {"type": "boolean"}},
            "required": ["title"]},
    },
    {
        "name": "todo_complete",
        "description": "Tick off a Microsoft To Do task by its id (from todo_list).",
        "input_schema": {"type": "object", "properties": {
            "task_id": {"type": "string"}, "list_name": {"type": "string"}, "account": {"type": "string"}},
            "required": ["task_id"]},
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


GOOGLE_TOOLS = {"gmail_search", "gmail_read", "gmail_draft", "gmail_send",
                "calendar_list", "calendar_add", "calendar_move"}


def needs_approval(name: str, args: dict) -> bool:
    if name in ("gmail_send", "calendar_add", "calendar_move"):
        return True  # always ask, even with AUTO_APPROVE_ALL
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
    acct = f" ({args['account']})" if args.get("account") else ""
    if name == "gmail_send":
        if args.get("draft_id"):
            return f"Send draft {args['draft_id']}{acct}"
        return f"Send email{acct}\nTo: {args.get('to')}\nSubject: {args.get('subject')}\n\n{args.get('body', '')[:600]}"
    if name == "calendar_add":
        extra = f"\nWhere: {args['location']}" if args.get("location") else ""
        inv = f"\nInvites: {args['attendees']}" if args.get("attendees") else ""
        return f"Add to calendar{acct}: {args.get('summary')}\nWhen: {args.get('start')} to {args.get('end') or '+1h'}{extra}{inv}"
    if name == "calendar_move":
        return f"Move event {args.get('event_id')}{acct} to {args.get('start')}"
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

        if name in ("todo_list", "todo_add", "todo_complete"):
            import mstodo
            return _cap(getattr(mstodo, name)(**args))

        if name in GOOGLE_TOOLS:
            import gapps
            return _cap(getattr(gapps, name)(**args))

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


def _claude_windows() -> list:
    """Visible top-level windows that belong to Claude.exe (the app runs several window-less helper processes)."""
    import ctypes
    import win32gui
    import win32process
    k32 = ctypes.windll.kernel32
    found = []

    def exe_of(pid):
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            return buf.value if ok else ""
        finally:
            k32.CloseHandle(h)

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if exe_of(pid).lower().endswith("\\claude.exe"):
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                found.append((hwnd, (r - l) * (b - t)))
        return True
    win32gui.EnumWindows(cb, None)
    return [h for h, _ in sorted(found, key=lambda x: -x[1])]  # biggest window first = the main one


def _focus(hwnd):
    import win32api
    import win32con
    import win32gui
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    # Windows only lets a program steal focus right after a key press, so tap Alt first
    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
    win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        win32gui.BringWindowToTop(hwnd)


def send_to_claude(message: str, send: bool = True) -> str:
    """Paste a message into the Claude desktop app's chat box and (optionally) press Enter."""
    import time
    import win32clipboard as cb
    import win32gui
    from pywinauto import keyboard, mouse

    wins = _claude_windows()
    if not wins:
        return ("Error: I can't see a Claude app window. If it's closed or hidden in the system tray, "
                "open it on the chat you want and ask again.")
    hwnd = wins[0]
    _focus(hwnd)
    time.sleep(0.7)

    cb.OpenClipboard()
    cb.EmptyClipboard()
    cb.SetClipboardText(message, cb.CF_UNICODETEXT)
    cb.CloseClipboard()

    # The message box sits at the bottom-centre of the window
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    offset = int(os.getenv("CLAUDE_INPUT_OFFSET", "110"))
    mouse.click(coords=((l + r) // 2, b - offset))
    time.sleep(0.3)
    keyboard.send_keys("^v")
    time.sleep(0.4)
    if send:
        keyboard.send_keys("{ENTER}")
        return f"Sent to Claude: {message[:120]}"
    return "Typed into Claude (not sent yet) so the user can review it."
