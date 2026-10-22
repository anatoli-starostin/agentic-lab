"""Secret intake: humans hand secrets to the lab without them passing through a chat.

The secretary (or the admin) creates a request for one secret, owned by a human (their
Claude token) or by a lab service (e.g. the broker's Slack app tokens). That yields a
one-time code. The secret then arrives here over the tailnet with the code, either from the
member client (`client/lab-member.py`) or from a one-time web form (`/s/<code>`) that a
human opens in their browser and pastes into. Codes are single-use, tied to one human and one secret name, and
expire (24 h by default). Only a hash of each code is stored.

Stored secrets live under the secretary service user's state directory, readable by that
user only. `lab apply` (M3a) distributes them, e.g. a human's Claude token to each of
their agents.
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets as pysecrets
import tempfile
import threading
import time
from dataclasses import dataclass

NAMES = ("claude-token", "slack-app", "nebius-sa", "github-token", "github-app")
# what each secret is made of, for the web form: (field, label, required prefix)
FIELDS = {
    "claude-token": [("value", "Claude token (sk-ant-…)", "sk-ant-")],
    "slack-app": [("bot", "Bot User OAuth Token (xoxb-…)", "xoxb-"),
                  ("app", "App-Level Token (xapp-…)", "xapp-")],
    # a Nebius service account's key, for VM power (the secretary's only)
    # a GitHub bot account's token (owner service:github-<login>): fine-grained or classic
    "github-token": [("value", "GitHub token (github_pat_… or ghp_…)", "")],
    # a GitHub App's private key (the .pem GitHub downloads), owner service:github-<slug>
    "github-app": [("private_key", "The app's private key (the .pem file's contents)",
                    "-----BEGIN")],
    "nebius-sa": [("service_account_id", "Service account id (serviceaccount-…)", "serviceaccount-"),
                  ("public_key_id", "Public key id (publickey-…)", "publickey-"),
                  ("private_key", "Private key (PEM)", "-----BEGIN")],
}
MULTILINE = {"private_key"}         # shown as a text area in the web form
_OWNER = re.compile(r"^(service:)?[a-z][a-z0-9-]*$")
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # no 0/O, 1/I
MAX_FAILURES = 20            # failed attempts per window, across all codes...
FAILURE_WINDOW_S = 600       # ...before the intake refuses everything for a while


class IntakeError(ValueError):
    pass


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _hash(code):
    return hashlib.sha256(code.encode()).hexdigest()


def _write_private(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


@dataclass
class Request:
    human: str
    name: str
    expires: str
    used: bool = False


def verify_github_token(token, owner=None, timeout=20):
    """Ask GitHub whose token this is: it must be the bot account it's for
    (owner service:github-<login>). Unreachable GitHub lets it through."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request("https://api.github.com/user", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "User-Agent": "agentic-lab"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            login = json.loads(r.read()).get("login", "")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise IntakeError("GitHub rejected this token: was all of it copied?") from None
        return
    except (urllib.error.URLError, OSError, ValueError):
        return
    want = (owner or "")[len("service:github-"):]
    if want and login.lower() != want:
        raise IntakeError(f"this is {login}'s token, not {want}'s: create it while logged "
                          f"in as {want}")


def verify_claude_token(token, owner=None, timeout=20):
    """Ask Anthropic whether `token` works, with a one-token request. Raises IntakeError if
    it's refused (e.g. cut short when copied); any other outcome (no network, an outage)
    lets the token through, since the lab can't tell it's bad."""
    import urllib.error
    import urllib.request
    body = json.dumps({"model": "claude-haiku-5-5", "max_tokens": 1,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "anthropic-version": "2023-06-01",
                 "anthropic-beta": "oauth-2025-04-20", "content-type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=timeout).close()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise IntakeError(f"Claude rejected this token ({len(token)} characters): "
                              "was all of it copied, every line?") from None
    except (urllib.error.URLError, OSError):
        pass


class Intake:
    def __init__(self, root, verify=None):
        """`verify`: name -> function(value) that raises IntakeError for a secret that
        doesn't work (the intake service checks Claude tokens; tests pass none)."""
        self.root = root
        self.verify = verify or {}
        self.requests_dir = os.path.join(root, "secret-requests")
        self.secrets_dir = os.path.join(root, "secrets")
        os.makedirs(self.requests_dir, mode=0o700, exist_ok=True)
        os.makedirs(self.secrets_dir, mode=0o700, exist_ok=True)
        self._lock = threading.Lock()
        self._failures = {}         # source (the caller's address) -> times of bad codes

    # ── requests ─────────────────────────────────────────────────────────────

    def request(self, human, name, ttl=dt.timedelta(hours=24)) -> str:
        """Create a one-time code to hand over secret `name`. `human` is the owner: a
        human id, or `service:<name>` for a lab service (e.g. `service:broker`)."""
        if name not in NAMES:
            raise IntakeError(f"unknown secret {name!r} (known: {NAMES})")
        if not _OWNER.match(human or ""):
            raise IntakeError(f"bad owner {human!r}")
        raw = "".join(pysecrets.choice(_ALPHABET) for _ in range(8))
        code = f"{raw[:4]}-{raw[4:]}"
        req = Request(human, name, (_now() + ttl).isoformat(timespec="seconds"))
        _write_private(os.path.join(self.requests_dir, _hash(code) + ".json"),
                       json.dumps(req.__dict__))
        return code

    def _too_many_failures(self, source):
        """Bad codes are counted per caller (#63): one device guessing can't lock out the
        others, who hold valid codes."""
        cutoff = time.monotonic() - FAILURE_WINDOW_S
        self._failures = {s: [t for t in ts if t > cutoff]
                          for s, ts in self._failures.items() if any(t > cutoff for t in ts)}
        return len(self._failures.get(source, [])) >= MAX_FAILURES

    def _failed(self, source):
        self._failures.setdefault(source, []).append(time.monotonic())

    def receive(self, code, name, value, source=None) -> str:
        """Store `value` if `code` is a live request for secret `name`. Returns the human.
        `source`: who's calling (their address), for the per-caller limit on bad codes."""
        with self._lock:
            if self._too_many_failures(source):
                raise IntakeError("too many failed attempts; try again later")
            path = os.path.join(self.requests_dir, _hash((code or "").strip().upper()) + ".json")
            try:
                with open(path) as f:
                    req = Request(**json.load(f))
            except (FileNotFoundError, ValueError):
                self._failed(source)
                raise IntakeError("unknown or already used code") from None
            if req.used:
                self._failed(source)
                raise IntakeError("unknown or already used code")
            if dt.datetime.fromisoformat(req.expires) < _now():
                raise IntakeError("this code has expired; ask for a new one")
            if not hmac.compare_digest(req.name, name or ""):
                raise IntakeError(f"this code is for {req.name}, not {name}")
            value = check_value(name, value)
            if name in self.verify:
                self.verify[name](value, req.human)
            self.store(req.human, name, value)
            req.used = True
            _write_private(path, json.dumps(req.__dict__))
            return req.human

    # ── stored secrets ───────────────────────────────────────────────────────

    def lookup(self, code):
        """The live request behind `code` (for the web form), or None. Doesn't use it up."""
        path = os.path.join(self.requests_dir, _hash((code or "").strip().upper()) + ".json")
        try:
            with open(path) as f:
                req = Request(**json.load(f))
        except (FileNotFoundError, ValueError):
            return None
        if req.used or dt.datetime.fromisoformat(req.expires) < _now():
            return None
        return req

    def _secret_path(self, owner, name):
        if not _OWNER.match(owner or "") or name not in NAMES:
            raise IntakeError("bad owner or secret name")
        if owner.startswith("service:"):
            return os.path.join(self.secrets_dir, "services", owner[8:], name)
        return os.path.join(self.secrets_dir, "humans", owner, name)

    def store(self, human, name, value):
        _write_private(self._secret_path(human, name), value.strip() + "\n")
        _write_private(self._secret_path(human, name) + ".meta",
                       json.dumps({"received": _now().isoformat(timespec="seconds")}))

    def load(self, human, name):
        try:
            with open(self._secret_path(human, name)) as f:
                return f.read().strip()
        except FileNotFoundError:
            return None


def check_value(name, value):
    """Validate a secret and return the text to store. A one-field secret is a string; a
    several-field one (slack-app) is a dict of its fields."""
    fields = FIELDS[name]
    if len(fields) == 1 and isinstance(value, str):
        value = {fields[0][0]: value}
    if not isinstance(value, dict):
        raise IntakeError("bad secret")
    out = {}
    for key, label, prefix in fields:
        v = (value.get(key) or "").strip()
        if not v:
            raise IntakeError(f"missing: {label}")
        if not v.startswith(prefix) or len(v) < 20:
            raise IntakeError(f"that doesn't look like a {label}")
        out[key] = v
    if name == "github-app":
        if "PRIVATE KEY-----" not in out["private_key"]:
            raise IntakeError("that doesn't look like a PEM private key")
        return out["private_key"]
    if name == "github-token" and not out["value"].startswith(("github_pat_", "ghp_")):
        raise IntakeError("that doesn't look like a GitHub token (github_pat_… or ghp_…)")
    if name == "claude-token" and len(out["value"]) < 40:
        raise IntakeError("that doesn't look like a Claude token")
    if name == "slack-app":
        return f"SLACK_BOT_TOKEN={out['bot']}\nSLACK_APP_TOKEN={out['app']}"
    if name == "nebius-sa":
        if "PRIVATE KEY-----" not in out["private_key"]:
            raise IntakeError("that doesn't look like a PEM private key")
        return json.dumps(out)
    return out["value"]


def form_url(code, url):
    """The one-time web form for a request."""
    return f"{url.rstrip('/')}/s/{code}"


def message(human_name, code, url, name="claude-token"):
    """The message the secretary sends the human (e.g. in a Slack DM)."""
    base = url.rstrip("/")
    return (
        f"Hi {human_name}! Your lab agents need your Claude seat token. On your computer "
        f"(on the lab's tailnet), please run:\n\n"
        f"curl -fsSL {base}/client/lab-member.py -o lab-member.py && "
        f"python3 lab-member.py {name} --code {code} --to {base}\n\n"
        f"It runs `claude setup-token` and sends the token straight to the lab, never "
        f"through chat. The code works once, within 24 hours.")
