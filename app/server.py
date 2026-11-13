"""Jarvis server: routes each request to Haiku / Sonnet / Opus and runs the tool loop."""
import asyncio
import os
import re
import time
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from paths import APP_DIR, ENV_FILE, load_settings, log, save_settings

load_dotenv(ENV_FILE)

from tools import TOOLS, describe_action, load_memory, needs_approval, run_tool  # noqa: E402
from voice import PRESETS, SentenceStream, Voice, current_preset  # noqa: E402

client = anthropic.Anthropic() if os.getenv("ANTHROPIC_API_KEY") else None
MODELS = {
    "fast": os.getenv("MODEL_FAST", "claude-haiku-5-5"),
    "smart": os.getenv("MODEL_SMART", "claude-sonnet-5-5"),
    "max": os.getenv("MODEL_MAX", "claude-opus-5-5"),
}
USER_NAME = os.getenv("USER_NAME", "Mahir")
MAX_HISTORY = 40
VERSION_FILE = APP_DIR / "version.json"

ROUTER_PROMPT = """You pick which model should handle a request to a personal assistant.
Reply with exactly one word:
fast  - chit-chat, quick facts, simple one-step actions (open an app, check something, a short answer)
smart - multi-step tasks, research, writing, editing files, anything needing several tools
max   - hard reasoning, complex analysis, big code changes, important documents or decisions
If unsure between two, pick the cheaper one."""

# Words that mean a request is more than a quick one-step job
HEAVY = re.compile(r"\b(research|analy[sz]e|analysis|compare|write|draft|plan|summari[sz]e|explain why|"
                   r"code|debug|fix|build|report|essay|email to|proposal|strategy|review|calculate|"
                   r"tell claude|ask claude|get claude)\b", re.I)

app = FastAPI()
sessions: dict = {}
clients: set = set()
loop = None
recent_notices: list = []


async def _broadcast(msg: dict):
    for ws in list(clients):
        try:
            await ws.send_json(msg)
        except Exception:
            clients.discard(ws)


def emit(msg: dict):
    if msg.get("type") == "notice":
        recent_notices.append(msg)
        del recent_notices[:-20]
        log(msg["text"])
    if loop:
        asyncio.run_coroutine_threadsafe(_broadcast(msg), loop)


voice = Voice(emit)
relay = None


@app.on_event("startup")
async def _startup():
    global loop
    loop = asyncio.get_running_loop()
    if os.getenv("VOICE", "true").lower() == "true":
        voice.start()
    global relay
    from relay import Relay
    relay = Relay(lambda text: chat(ChatIn(session="phone", text=text, client="jarvis-app")),
                  lambda aid, ok: approve(ApproveIn(session="phone", id=aid or "", approve=ok)),
                  emit, tts=voice.mp3)
    relay.start()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    for m in recent_notices:  # catch the window up on anything it missed while opening
        await ws.send_json(m)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.discard(ws)


def system_prompt() -> str:
    mem = load_memory()
    mem_text = "\n".join(f"- {m['note']}" for m in mem[-50:]) or "(nothing saved yet)"
    p = current_preset()
    return f"""You are {p['name']}, {USER_NAME}'s personal AI assistant, running on his Windows PC. Your manner: {p['about']}, quietly witty, never gushing.
Home folder: {Path.home()}.

How to behave:
- Replies are usually read aloud, so keep them short and conversational: 1-3 sentences unless asked for detail. No markdown tables or long lists in spoken replies.
- Plain, direct tone. Call him {USER_NAME} occasionally, not every reply. No "sir".
- Act, don't ask: if a request is clear, use your tools and then report what you did.
- For any action (opening, running, checking, saving), you MUST actually call a tool. Never say you did something unless a tool call in this turn did it and succeeded. If a tool errors, say so plainly.
- Spoken input comes from speech recognition and may have small mistakes ("open spot if I" = "open Spotify"). Use common sense.
- The PC runs Windows; use PowerShell syntax.
- Risky actions (deleting, installing, writing files, changing settings) go to {USER_NAME} for approval automatically, so just call the tool.
- You ARE Claude (Anthropic's model), running in the cloud through {USER_NAME}'s API key, and you can search the web yourself. So answer questions, look things up (restaurants, news, prices) and do tasks directly with your own tools. Never say you can't reach Claude or the cloud.
- `send_to_claude` only types a message into the separate Claude desktop chat app on this PC, where {USER_NAME} runs long projects (coding, documents). Use it ONLY when he explicitly says "tell Claude / ask Claude / send this to Claude" about work in that app; then pass the request on clearly with useful detail.
- If an answer depends on where he is and you don't know it, ask once, then `remember` it.
- "Switch to Friday / Edith / Jarvis" means use `set_voice`.
- If he wraps up ("that's all", "thanks"), reply with a brief sign-off.
- Use `remember` when he tells you something lasting about himself or how he wants things done.

Google accounts connected (Gmail + Calendar): {_google_accounts()}
- Email: search/read freely; save replies as drafts. Only use gmail_send when he explicitly says to send.
Microsoft To Do accounts connected: {_ms_accounts()}
- "My to-dos / add to my list / tick off" means Microsoft To Do (todo_* tools).
- Calendar: times are local ({os.getenv('TIMEZONE', 'Asia/Kolkata')}). Check calendar_list before suggesting a slot.

What you know about {USER_NAME}:
{mem_text}"""


def system_blocks() -> list:
    """Big stable part is cached (much faster first token); the clock goes in a small block after it."""
    return [{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": f"Now: {datetime.now().strftime('%A %d %B %Y, %I:%M %p')}."}]


def _google_accounts() -> str:
    try:
        import gapps
        a = gapps.status()["accounts"]
        return ", ".join(f"{x['email']} ({x['label']})" for x in a) or "none yet"
    except Exception:
        return "none yet"


def _ms_accounts() -> str:
    try:
        import mstodo
        a = mstodo.status()["accounts"]
        return ", ".join(f"{x['email']} ({x['label']})" for x in a) or "none yet"
    except Exception:
        return "none yet"


def pick_tier(text: str) -> str:
    t = text.lower()
    if re.search(r"\b(use opus|think hard|max mode)\b", t):
        return "max"
    if re.search(r"\b(use sonnet|smart mode)\b", t):
        return "smart"
    if re.search(r"\b(use haiku|quick mode)\b", t):
        return "fast"
    # Short, simple commands go straight to Haiku - saves a round trip
    if len(t.split()) <= 18 and not HEAVY.search(t):
        return "fast"
    try:
        r = client.messages.create(model=MODELS["fast"], max_tokens=5, system=ROUTER_PROMPT,
                                   messages=[{"role": "user", "content": text[:2000]}])
        word = r.content[0].text.strip().lower().split()[0]
        return word if word in MODELS else "smart"
    except Exception:
        return "smart"


def trim(messages: list) -> list:
    """Keep recent history, cutting only at a plain user message so tool pairs stay intact."""
    if len(messages) <= MAX_HISTORY:
        return messages
    for i in range(len(messages) - MAX_HISTORY, len(messages)):
        m = messages[i]
        if m["role"] == "user" and isinstance(m["content"], str):
            return messages[i:]
    return messages


def text_of(content) -> str:
    return "".join(getattr(b, "text", "") for b in content if getattr(b, "type", "") == "text").strip()


def agent_loop(s: dict, say: SentenceStream | None = None) -> dict:
    """Call Claude until it gives a final answer, or pause when an action needs approval."""
    model = MODELS[s["tier"]]
    for _ in range(25):
        pending = s.get("pending")
        if pending:  # resuming after approval: finish remaining tool calls first
            blocks, results = pending["blocks"], pending["results"]
            s["pending"] = None
        else:
            s["messages"] = trim(s["messages"])
            t0 = time.time()
            kw = dict(model=model, max_tokens=4096, system=system_blocks(), tools=TOOLS, messages=s["messages"])
            if say:  # speak sentences as they arrive instead of waiting for the whole reply
                with client.messages.stream(**kw) as stream:
                    for ev in stream:
                        if ev.type == "content_block_delta" and getattr(ev.delta, "type", "") == "text_delta":
                            say.push(ev.delta.text)
                    resp = stream.get_final_message()
                say.flush()
            else:
                resp = client.messages.create(**kw)
            s["timings"].append(f"{s['tier']} {int((time.time() - t0) * 1000)}ms")
            s["messages"].append({"role": "assistant", "content": resp.content})
            s["actions"] += [f"web_search({b.input.get('query', '')})" for b in resp.content
                             if b.type == "server_tool_use"]
            if resp.stop_reason == "pause_turn":
                continue
            blocks = [b for b in resp.content if b.type == "tool_use"]
            if not blocks:
                return {"reply": text_of(resp.content) or "Done.", "model": model, "tier": s["tier"],
                        "actions": s["actions"]}
            results = []

        while len(results) < len(blocks):
            b = blocks[len(results)]
            if needs_approval(b.name, b.input) and not s.pop("approved_id", None) == b.id:
                s["pending"] = {"blocks": blocks, "results": results}
                return {"approval": {"id": b.id, "description": describe_action(b.name, b.input)},
                        "model": model, "tier": s["tier"]}
            if s.pop("denied_id", None) == b.id:
                out = "The user declined this action. Don't retry it; ask what they'd like instead."
            else:
                t0 = time.time()
                out = run_tool(b.name, b.input)
                s["timings"].append(f"{b.name} {int((time.time() - t0) * 1000)}ms")
                arg = next(iter(b.input.values()), "") if b.input else ""
                s["actions"].append(f"{b.name}({str(arg)[:60]})" + (" ✗" if out.startswith("Error") else ""))
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})

        s["messages"].append({"role": "user", "content": results})
    return {"reply": "I hit my step limit on that one. Want me to keep going?", "model": model,
            "tier": s["tier"]}


def run_turn(s: dict, fn) -> dict:
    s["timings"] = []
    t0 = time.time()
    if client is None:
        return {"reply": "I need your Anthropic API key first. Paste it in the box on the right.", "tier": "fast"}
    try:
        res = fn()
    except anthropic.APIError as e:
        res = {"reply": f"Claude API error: {getattr(e, 'message', e)}", "tier": s.get("tier")}
    total = int((time.time() - t0) * 1000)
    log(f"timing  total {total} ms  [{', '.join(s['timings'])}]")
    res["ms"] = total
    return res


class ChatIn(BaseModel):
    session: str
    text: str
    client: str | None = None  # the current app sends "jarvis-app"; old browser tabs don't
    voice: bool = False        # spoken request: stream the answer straight into the voice

OLD_TAB = {"reply": "This is an old Jarvis browser tab. Please close it. Jarvis now runs in its own window.",
           "tier": "fast"}


class ApproveIn(BaseModel):
    session: str
    id: str
    approve: bool


@app.post("/api/chat")
def chat(body: ChatIn):
    if body.client != "jarvis-app":
        return OLD_TAB
    s = sessions.setdefault(body.session, {"messages": [], "pending": None, "tier": "smart",
                                           "actions": [], "timings": []})
    if s.get("pending"):  # a new message cancels an unanswered approval
        blocks, results = s["pending"]["blocks"], s["pending"]["results"]
        for b in blocks[len(results):]:
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": "Cancelled: the user moved on without approving."})
        s["messages"].append({"role": "user", "content": results})
        s["pending"] = None

    def go():
        t0 = time.time()
        s["tier"] = pick_tier(body.text)
        s["timings"].append(f"route {int((time.time() - t0) * 1000)}ms")
        s["actions"] = []
        s["messages"].append({"role": "user", "content": body.text})
        return agent_loop(s, say)

    st = voice.begin() if body.voice and voice.ready else None
    say = SentenceStream(voice, st) if st else None
    try:
        res = run_turn(s, go)
    finally:
        if st:
            voice.end(st)
    if st and st["chars"] and not res.get("approval"):
        res["spoken"] = True
    return res


@app.post("/api/approve")
def approve(body: ApproveIn):
    s = sessions.get(body.session)
    if not s or not s.get("pending"):
        return {"reply": "There's nothing waiting for approval."}
    s["approved_id"] = body.id  # lets the loop pass this tool
    if not body.approve:
        s["denied_id"] = body.id  # ...and records it as declined instead of running it
    return run_turn(s, lambda: agent_loop(s))


# ---------- voice + settings ----------
class SpeakIn(BaseModel):
    text: str


class PttIn(BaseModel):
    down: bool


class SettingsIn(BaseModel):
    anthropic_key: str | None = None
    voice_preset: str | None = None
    relay_url: str | None = None
    relay_token: str | None = None


@app.post("/api/speak")
def speak(body: SpeakIn):
    voice.speak(body.text)
    return {"ok": True}


@app.post("/api/voice/ptt")
def ptt(body: PttIn):
    voice.ptt(body.down)
    return {"ok": True}


@app.post("/api/voice/listen")
def listen():
    voice.listen_now()
    return {"ok": True}


@app.post("/api/voice/stop")
def stop_conversation():
    voice.end_conversation()
    return {"ok": True}


@app.post("/api/settings")
def settings(body: SettingsIn):
    global client
    if body.anthropic_key:
        key = body.anthropic_key.strip()
        lines = [l for l in (ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else [])
                 if not l.startswith("ANTHROPIC_API_KEY=")]
        ENV_FILE.write_text("\n".join([f"ANTHROPIC_API_KEY={key}"] + lines) + "\n", encoding="utf-8")
        os.environ["ANTHROPIC_API_KEY"] = key
        client = anthropic.Anthropic()
    if body.voice_preset in PRESETS:
        save_settings(voice_preset=body.voice_preset)
    if body.relay_url is not None:
        save_settings(relay_url=body.relay_url.strip().rstrip("/"))
    if body.relay_token:
        save_settings(relay_token=body.relay_token.strip())
    return health()


@app.get("/api/health")
def health():
    v = {}
    try:
        import json
        v = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"ok": True, "models": MODELS, "key_set": client is not None, "voice_ready": voice.ready,
            "preset": current_preset()["key"],
            "relay": {"url": load_settings().get("relay_url", ""), "connected": bool(relay and relay.ok)}, "presets": list(PRESETS), "version": v.get("version", "dev")}


@app.post("/api/update")
def update():
    """Check the update mirror and download new app files. Applied on restart."""
    try:
        import updater
        return updater.update_app_files(APP_DIR)
    except Exception as e:
        return {"updated": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/restart")
def restart():
    import sys
    import threading

    def later():
        import subprocess
        time.sleep(0.5)
        args = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, sys.argv[0]]
        subprocess.Popen(args + ["--restarted"], close_fds=True)
        os._exit(0)
    threading.Thread(target=later, daemon=True).start()
    return {"ok": True}


# ---------- Google ----------
class GoogleClientIn(BaseModel):
    json_text: str


@app.get("/api/google")
def google_status():
    import gapps
    return gapps.status()


@app.post("/api/google/client")
def google_client(body: GoogleClientIn):
    import gapps
    try:
        gapps.save_client(body.json_text)
        return {"ok": True, **gapps.status()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/google/connect")
def google_connect():
    """Opens Google sign-in in the default browser."""
    import gapps
    try:
        url = gapps.auth_url()
        if os.name == "nt":
            os.startfile(url)
        else:
            import webbrowser
            webbrowser.open(url)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/oauth/google/callback")
def google_callback(code: str = "", state: str = "", error: str = ""):
    import gapps
    page = ("<html><body style='background:#05070d;color:#e6ecff;font-family:Segoe UI;display:grid;"
            "place-items:center;height:100vh'><div style='text-align:center'><h2>{}</h2><p>{}</p></div></body></html>")
    if error:
        return HTMLResponse(page.format("Google sign-in cancelled", error))
    try:
        email = gapps.finish_auth(code, state)
        emit({"type": "notice", "text": f"Google connected: {email}"})
        emit({"type": "google"})
        return HTMLResponse(page.format(f"Connected {email}", "You can close this tab and go back to Jarvis."))
    except Exception as e:
        return HTMLResponse(page.format("Couldn't connect", str(e)))


# ---------- Microsoft To Do ----------
class MsClientIn(BaseModel):
    client_id: str


@app.get("/api/microsoft")
def ms_status():
    import mstodo
    return mstodo.status()


@app.post("/api/microsoft/client")
def ms_client(body: MsClientIn):
    import mstodo
    try:
        mstodo.save_client(body.client_id)
        return {"ok": True, **mstodo.status()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/microsoft/connect")
def ms_connect():
    import mstodo
    try:
        url = mstodo.auth_url()
        if os.name == "nt":
            os.startfile(url)
        else:
            import webbrowser
            webbrowser.open(url)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/oauth/microsoft/callback")
def ms_callback(code: str = "", state: str = "", error: str = "", error_description: str = ""):
    import mstodo
    page = ("<html><body style='background:#05070d;color:#e6ecff;font-family:Segoe UI;display:grid;"
            "place-items:center;height:100vh'><div style='text-align:center'><h2>{}</h2><p>{}</p></div></body></html>")
    if error:
        return HTMLResponse(page.format("Microsoft sign-in didn't finish", error_description or error))
    try:
        email = mstodo.finish_auth(code, state)
        emit({"type": "notice", "text": f"Microsoft To Do connected: {email}"})
        emit({"type": "google"})  # refreshes the Accounts panel
        return HTMLResponse(page.format(f"Connected {email}", "You can close this tab and go back to Jarvis."))
    except Exception as e:
        return HTMLResponse(page.format("Couldn't connect", str(e)))


app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(APP_DIR / "static" / "index.html")
