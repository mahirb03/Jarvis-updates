"""Microsoft To Do for Jarvis (personal + work Microsoft accounts) via Microsoft Graph. Plain REST, no extra packages.

Uses a "public client" app registration (no secret) with PKCE. The client id isn't secret.
Set it once in Jarvis (Accounts panel) or with MS_CLIENT_ID in .env.
"""
import base64
import hashlib
import json
import os
import time
import urllib.parse

import httpx

from paths import DATA_DIR

CLIENT_FILE = DATA_DIR / "microsoft_client.json"
ACCOUNTS_FILE = DATA_DIR / "microsoft_accounts.json"
REDIRECT = "http://localhost:8765/oauth/microsoft/callback"
AUTH = "https://login.microsoftonline.com/common/oauth2/v2.0"
SCOPES = "offline_access User.Read Tasks.ReadWrite"
GRAPH = "https://graph.microsoft.com/v1.0"
TZ = os.getenv("WINDOWS_TIMEZONE", "India Standard Time")
_pending: dict = {}   # state -> code_verifier
_tokens: dict = {}    # account -> (access, expires_at)


# ---------- setup / auth ----------
def client_id() -> str:
    cid = os.getenv("MS_CLIENT_ID")
    if not cid and CLIENT_FILE.exists():
        cid = json.loads(CLIENT_FILE.read_text(encoding="utf-8")).get("client_id")
    if not cid:
        raise RuntimeError("Microsoft isn't set up yet: add the Jarvis app's client ID in the Accounts panel.")
    return cid


def save_client(cid: str) -> str:
    cid = cid.strip()
    if len(cid) < 30 or cid.count("-") != 4:
        raise ValueError("That doesn't look like an Application (client) ID.")
    CLIENT_FILE.write_text(json.dumps({"client_id": cid}), encoding="utf-8")
    return "saved"


def _accounts() -> dict:
    try:
        return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_accounts(a: dict):
    ACCOUNTS_FILE.write_text(json.dumps(a, indent=2), encoding="utf-8")


def status() -> dict:
    try:
        configured = bool(client_id())
    except Exception:
        configured = False
    return {"client": configured,
            "accounts": [{"email": e, "label": v.get("label")} for e, v in _accounts().items()]}


def auth_url() -> str:
    verifier = base64.urlsafe_b64encode(os.urandom(40)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
    _pending[state] = verifier
    q = {"client_id": client_id(), "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPES,
         "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
         "prompt": "select_account"}
    return f"{AUTH}/authorize?" + urllib.parse.urlencode(q)


def finish_auth(code: str, state: str) -> str:
    verifier = _pending.pop(state, None)
    if not verifier:
        raise RuntimeError("This sign-in link expired. Click Connect again.")
    r = httpx.post(f"{AUTH}/token", data={
        "client_id": client_id(), "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT, "code_verifier": verifier, "scope": SCOPES}, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Microsoft sign-in failed: {r.text[:300]}")
    tok = r.json()
    me = httpx.get(f"{GRAPH}/me", headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=20).json()
    email = (me.get("mail") or me.get("userPrincipalName") or "account").lower()
    personal = any(email.endswith(d) for d in ("@outlook.com", "@hotmail.com", "@live.com", "@msn.com"))
    acc = _accounts()
    acc[email] = {"refresh_token": tok["refresh_token"],
                  "label": acc.get(email, {}).get("label") or ("personal" if personal else "work")}
    _save_accounts(acc)
    _tokens[email] = (tok["access_token"], time.time() + tok.get("expires_in", 3600) - 60)
    return email


def _pick(account: str | None) -> str:
    acc = _accounts()
    if not acc:
        raise RuntimeError("No Microsoft account connected yet.")
    if not account:
        return next(iter(acc))
    a = account.lower().strip()
    for email, v in acc.items():
        if a in (email, (v.get("label") or "").lower()) or a in email:
            return email
    raise RuntimeError(f"No connected Microsoft account matches '{account}'. Connected: {', '.join(acc)}")


def _token(email: str) -> str:
    t = _tokens.get(email)
    if t and t[1] > time.time():
        return t[0]
    acc = _accounts()
    r = httpx.post(f"{AUTH}/token", data={
        "client_id": client_id(), "grant_type": "refresh_token",
        "refresh_token": acc[email]["refresh_token"], "scope": SCOPES}, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Microsoft sign-in for {email} has expired. Reconnect it in Jarvis.")
    tok = r.json()
    if tok.get("refresh_token"):  # Microsoft rotates refresh tokens
        acc[email]["refresh_token"] = tok["refresh_token"]
        _save_accounts(acc)
    _tokens[email] = (tok["access_token"], time.time() + tok.get("expires_in", 3600) - 60)
    return tok["access_token"]


def _api(method, path, email, **kw):
    r = httpx.request(method, GRAPH + path, headers={"Authorization": f"Bearer {_token(email)}"}, timeout=30, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"Microsoft Graph {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def _lists(email):
    return _api("GET", "/me/todo/lists", email).get("value", [])


def _find_list(email, name):
    lists = _lists(email)
    if not name:
        for l in lists:
            if l.get("wellknownListName") == "defaultList":
                return l
        return lists[0]
    n = name.lower()
    for l in lists:
        if l["displayName"].lower() == n:
            return l
    for l in lists:
        if n in l["displayName"].lower():
            return l
    raise RuntimeError(f"No list called '{name}'. Lists: {', '.join(l['displayName'] for l in lists)}")


# ---------- tools ----------
def todo_list(list_name: str | None = None, account: str | None = None, include_done: bool = False) -> str:
    """Open tasks. No list_name = every list. No account = every connected account."""
    emails = [_pick(account)] if account else list(_accounts())
    if not emails:
        raise RuntimeError("No Microsoft account connected yet.")
    out = []
    for email in emails:
        lists = [_find_list(email, list_name)] if list_name else _lists(email)
        for l in lists:
            params = {} if include_done else {"$filter": "status ne 'completed'"}
            tasks = _api("GET", f"/me/todo/lists/{l['id']}/tasks", email, params={**params, "$top": 50}).get("value", [])
            for t in tasks:
                due = t.get("dueDateTime", {}).get("dateTime", "")[:10]
                imp = " !" if t.get("importance") == "high" else ""
                done = " (done)" if t.get("status") == "completed" else ""
                out.append(f"[{email} | {l['displayName']} | {t['id']}] {t['title']}{imp}"
                           f"{' due ' + due if due else ''}{done}")
    return "\n".join(out) or "No open to-dos."


def todo_add(title: str, list_name: str | None = None, account: str | None = None, due: str | None = None,
             notes: str | None = None, important: bool = False) -> str:
    email = _pick(account)
    l = _find_list(email, list_name)
    task = {"title": title, "importance": "high" if important else "normal"}
    if due:
        task["dueDateTime"] = {"dateTime": (due if "T" in due else due + "T09:00:00"), "timeZone": TZ}
    if notes:
        task["body"] = {"content": notes, "contentType": "text"}
    _api("POST", f"/me/todo/lists/{l['id']}/tasks", email, json=task)
    return f"Added '{title}' to {l['displayName']} ({email})" + (f", due {due[:10]}" if due else "")


def todo_complete(task_id: str, list_name: str | None = None, account: str | None = None) -> str:
    """task_id from todo_list. Searches the given list, or all lists."""
    email = _pick(account)
    lists = [_find_list(email, list_name)] if list_name else _lists(email)
    for l in lists:
        try:
            t = _api("PATCH", f"/me/todo/lists/{l['id']}/tasks/{task_id}", email, json={"status": "completed"})
            return f"Ticked off '{t.get('title')}'."
        except RuntimeError as e:
            if "404" not in str(e):
                raise
    raise RuntimeError("Couldn't find that task. List the to-dos again and use the id shown.")
