"""Gmail + Google Calendar for Jarvis (several Google accounts, plain REST - no extra packages).

Setup (once): a Google Cloud OAuth client of type "Desktop app". Paste its JSON into Jarvis,
then click "Connect Google account" for each account. Tokens live in your Jarvis data folder.
"""
import base64
import json
import os

import time
import urllib.parse
from datetime import datetime, timedelta
from email.message import EmailMessage

import httpx

from paths import DATA_DIR

CLIENT_FILE = DATA_DIR / "google_client.json"
ACCOUNTS_FILE = DATA_DIR / "google_accounts.json"
REDIRECT = "http://127.0.0.1:8765/oauth/google/callback"
TZ = os.getenv("TIMEZONE", "Asia/Kolkata")
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",   # drafts + sending (sending always asks you first)
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CAL = "https://www.googleapis.com/calendar/v3"
_states: dict = {}
_tokens: dict = {}  # email -> (access_token, expires_at)


# ---------- setup / auth ----------
def save_client(raw: str) -> str:
    data = json.loads(raw)
    c = data.get("installed") or data.get("web") or data
    if not c.get("client_id") or not c.get("client_secret"):
        raise ValueError("That doesn't look like a Google OAuth client JSON.")
    CLIENT_FILE.write_text(json.dumps({"client_id": c["client_id"], "client_secret": c["client_secret"]}),
                           encoding="utf-8")
    return "saved"


def _client() -> dict:
    if not CLIENT_FILE.exists():
        raise RuntimeError("Google isn't set up yet: paste the OAuth client JSON in Jarvis first.")
    return json.loads(CLIENT_FILE.read_text(encoding="utf-8"))


def _accounts() -> dict:
    try:
        return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_accounts(a: dict):
    ACCOUNTS_FILE.write_text(json.dumps(a, indent=2), encoding="utf-8")


def status() -> dict:
    return {"client": CLIENT_FILE.exists(),
            "accounts": [{"email": e, "label": v.get("label")} for e, v in _accounts().items()]}


def auth_url() -> str:
    c = _client()
    state = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
    _states[state] = time.time()
    q = {"client_id": c["client_id"], "redirect_uri": REDIRECT, "response_type": "code",
         "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent select_account",
         "include_granted_scopes": "true", "state": state}
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(q)


def finish_auth(code: str, state: str) -> str:
    if state not in _states:
        raise RuntimeError("This sign-in link expired. Click Connect again.")
    _states.pop(state, None)
    c = _client()
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": c["client_id"], "client_secret": c["client_secret"],
        "redirect_uri": REDIRECT, "grant_type": "authorization_code"}, timeout=20)
    r.raise_for_status()
    tok = r.json()
    access = tok["access_token"]
    email = httpx.get(f"{GMAIL}/profile", headers={"Authorization": f"Bearer {access}"},
                      timeout=20).json()["emailAddress"]
    acc = _accounts()
    old = acc.get(email, {})
    acc[email] = {"refresh_token": tok.get("refresh_token") or old.get("refresh_token"),
                  "label": old.get("label") or ("personal" if email.endswith("@gmail.com") else "work")}
    _save_accounts(acc)
    _tokens[email] = (access, time.time() + tok.get("expires_in", 3600) - 60)
    return email


def _pick(account: str | None) -> str:
    acc = _accounts()
    if not acc:
        raise RuntimeError("No Google account connected yet.")
    if not account:
        return next(iter(acc))
    a = account.lower().strip()
    for email, v in acc.items():
        if a in (email.lower(), (v.get("label") or "").lower()) or a in email.lower():
            return email
    raise RuntimeError(f"No connected account matches '{account}'. Connected: {', '.join(acc)}")


def _token(email: str) -> str:
    t = _tokens.get(email)
    if t and t[1] > time.time():
        return t[0]
    c, acc = _client(), _accounts()
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "client_id": c["client_id"], "client_secret": c["client_secret"],
        "refresh_token": acc[email]["refresh_token"], "grant_type": "refresh_token"}, timeout=20)
    if r.status_code == 400:
        raise RuntimeError(f"Google sign-in for {email} has expired. Reconnect it in Jarvis.")
    r.raise_for_status()
    tok = r.json()
    _tokens[email] = (tok["access_token"], time.time() + tok.get("expires_in", 3600) - 60)
    return tok["access_token"]


def _api(method: str, url: str, email: str, **kw):
    r = httpx.request(method, url, headers={"Authorization": f"Bearer {_token(email)}"}, timeout=30, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"Google API {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


# ---------- Gmail ----------
def _hdr(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _body(payload: dict) -> str:
    def walk(p):
        if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data"):
            yield base64.urlsafe_b64decode(p["body"]["data"] + "==").decode("utf-8", "replace")
        for sub in p.get("parts", []) or []:
            yield from walk(sub)
    text = "\n".join(walk(payload))
    if not text and payload.get("body", {}).get("data"):  # html-only email
        import re
        html = base64.urlsafe_b64decode(payload["body"]["data"] + "==").decode("utf-8", "replace")
        text = re.sub(r"<[^>]+>", " ", html)
    return text.strip()


def gmail_search(query: str, account: str | None = None, max_results: int = 10) -> str:
    email = _pick(account)
    res = _api("GET", f"{GMAIL}/messages", email, params={"q": query, "maxResults": min(max_results, 25)})
    ids = [m["id"] for m in res.get("messages", [])]
    if not ids:
        return f"No emails in {email} match: {query}"
    out = []
    for i in ids:
        m = _api("GET", f"{GMAIL}/messages/{i}", email,
                 params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})
        out.append(f"[{i}] {_hdr(m, 'Date')} | From: {_hdr(m, 'From')} | {_hdr(m, 'Subject')}\n    {m.get('snippet', '')}")
    return f"{email}:\n" + "\n".join(out)


def gmail_read(message_id: str, account: str | None = None) -> str:
    email = _pick(account)
    m = _api("GET", f"{GMAIL}/messages/{message_id}", email, params={"format": "full"})
    body = _body(m.get("payload", {}))
    return (f"From: {_hdr(m, 'From')}\nTo: {_hdr(m, 'To')}\nDate: {_hdr(m, 'Date')}\n"
            f"Subject: {_hdr(m, 'Subject')}\n\n{body[:6000]}")


def _raw(to, subject, body, cc=None, reply_headers=None):
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    for k, v in (reply_headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def gmail_draft(body: str, to: str | None = None, subject: str | None = None, account: str | None = None, cc: str | None = None,
                reply_to_message_id: str | None = None) -> str:
    email = _pick(account)
    message, reply_headers = {}, None
    if reply_to_message_id:
        orig = _api("GET", f"{GMAIL}/messages/{reply_to_message_id}", email,
                    params={"format": "metadata", "metadataHeaders": ["Message-ID", "Subject", "From", "References"]})
        mid = _hdr(orig, "Message-ID")
        reply_headers = {"In-Reply-To": mid, "References": (_hdr(orig, "References") + " " + mid).strip()}
        message["threadId"] = orig["threadId"]
        to = to or _hdr(orig, "From")
        subject = subject or ("Re: " + _hdr(orig, "Subject"))
    if not to:
        raise RuntimeError("Who should the draft go to? Give 'to', or a message to reply to.")
    message["raw"] = _raw(to, subject or "", body, cc, reply_headers)
    d = _api("POST", f"{GMAIL}/drafts", email, json={"message": message})
    return f"Draft saved in {email} (draft id {d['id']}) to {to}: {subject}"


def gmail_send(account: str | None = None, draft_id: str | None = None, to: str | None = None,
               subject: str | None = None, body: str | None = None) -> str:
    email = _pick(account)
    if draft_id:
        _api("POST", f"{GMAIL}/drafts/send", email, json={"id": draft_id})
        return f"Sent draft {draft_id} from {email}."
    if not (to and subject and body):
        raise RuntimeError("To send, give a draft_id, or to + subject + body.")
    _api("POST", f"{GMAIL}/messages/send", email, json={"raw": _raw(to, subject, body)})
    return f"Sent from {email} to {to}: {subject}"


# ---------- Calendar ----------
def _when(e: dict, k: str) -> str:
    v = e.get(k, {})
    return v.get("dateTime", v.get("date", ""))


def calendar_list(start: str | None = None, end: str | None = None, account: str | None = None,
                  query: str | None = None) -> str:
    """start/end: ISO dates or datetimes. Defaults to today through 7 days."""
    now = datetime.now()
    s = start or now.strftime("%Y-%m-%dT00:00:00")
    e = end or (now + timedelta(days=7)).strftime("%Y-%m-%dT23:59:59")
    s = s if "T" in s else s + "T00:00:00"
    e = e if "T" in e else e + "T23:59:59"
    emails = [_pick(account)] if account else list(_accounts())
    out = []
    for email in emails:
        params = {"timeMin": s + _offset(), "timeMax": e + _offset(), "singleEvents": "true",
                  "orderBy": "startTime", "maxResults": 50, "timeZone": TZ}
        if query:
            params["q"] = query
        res = _api("GET", f"{CAL}/calendars/primary/events", email, params=params)
        for ev in res.get("items", []):
            loc = f" @ {ev['location']}" if ev.get("location") else ""
            out.append(f"[{email} | {ev['id']}] {_when(ev, 'start')} to {_when(ev, 'end')}: "
                       f"{ev.get('summary', '(no title)')}{loc}")
    return "\n".join(out) or f"Nothing on the calendar between {s[:10]} and {e[:10]}."


def _offset() -> str:
    return "+05:30" if TZ == "Asia/Kolkata" else "Z"


def calendar_add(summary: str, start: str, end: str | None = None, account: str | None = None,
                 location: str | None = None, description: str | None = None, attendees: str | None = None) -> str:
    email = _pick(account)
    st = datetime.fromisoformat(start)
    en = datetime.fromisoformat(end) if end else st + timedelta(hours=1)
    ev = {"summary": summary, "start": {"dateTime": st.isoformat(), "timeZone": TZ},
          "end": {"dateTime": en.isoformat(), "timeZone": TZ}}
    if location:
        ev["location"] = location
    if description:
        ev["description"] = description
    if attendees:
        ev["attendees"] = [{"email": a.strip()} for a in attendees.split(",") if a.strip()]
    r = _api("POST", f"{CAL}/calendars/primary/events", email, json=ev)
    return f"Added to {email}'s calendar: {summary}, {st:%a %d %b %I:%M %p} (event id {r['id']})"


def calendar_move(event_id: str, start: str, end: str | None = None, account: str | None = None) -> str:
    email = _pick(account)
    cur = _api("GET", f"{CAL}/calendars/primary/events/{event_id}", email)
    st = datetime.fromisoformat(start)
    if end:
        en = datetime.fromisoformat(end)
    else:  # keep the same length
        try:
            dur = datetime.fromisoformat(_when(cur, "end")[:19]) - datetime.fromisoformat(_when(cur, "start")[:19])
        except Exception:
            dur = timedelta(hours=1)
        en = st + dur
    _api("PATCH", f"{CAL}/calendars/primary/events/{event_id}", email,
         json={"start": {"dateTime": st.isoformat(), "timeZone": TZ}, "end": {"dateTime": en.isoformat(), "timeZone": TZ}})
    return f"Moved '{cur.get('summary')}' to {st:%a %d %b %I:%M %p}."
