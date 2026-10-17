"""Secret intake: humans hand secrets to the lab without them passing through a chat.

The secretary (or the admin) creates a request for one human and one secret. That yields a
one-time code and a ready-to-send message. The human runs the member client
(`client/lab-member.py`) on their own machine, which sends the secret here over the
tailnet with the code. Codes are single-use, tied to one human and one secret name, and
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
import secrets as pysecrets
import tempfile
import threading
import time
from dataclasses import dataclass

NAMES = ("claude-token",)
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


class Intake:
    def __init__(self, root):
        self.root = root
        self.requests_dir = os.path.join(root, "secret-requests")
        self.secrets_dir = os.path.join(root, "secrets")
        os.makedirs(self.requests_dir, mode=0o700, exist_ok=True)
        os.makedirs(self.secrets_dir, mode=0o700, exist_ok=True)
        self._lock = threading.Lock()
        self._failures = []

    # ── requests ─────────────────────────────────────────────────────────────

    def request(self, human, name, ttl=dt.timedelta(hours=24)) -> str:
        """Create a one-time code for `human` to hand over secret `name`."""
        if name not in NAMES:
            raise IntakeError(f"unknown secret {name!r} (known: {NAMES})")
        raw = "".join(pysecrets.choice(_ALPHABET) for _ in range(8))
        code = f"{raw[:4]}-{raw[4:]}"
        req = Request(human, name, (_now() + ttl).isoformat(timespec="seconds"))
        _write_private(os.path.join(self.requests_dir, _hash(code) + ".json"),
                       json.dumps(req.__dict__))
        return code

    def _too_many_failures(self):
        cutoff = time.monotonic() - FAILURE_WINDOW_S
        self._failures = [t for t in self._failures if t > cutoff]
        return len(self._failures) >= MAX_FAILURES

    def receive(self, code, name, value) -> str:
        """Store `value` if `code` is a live request for secret `name`. Returns the human."""
        with self._lock:
            if self._too_many_failures():
                raise IntakeError("too many failed attempts; try again later")
            path = os.path.join(self.requests_dir, _hash((code or "").strip().upper()) + ".json")
            try:
                with open(path) as f:
                    req = Request(**json.load(f))
            except (FileNotFoundError, ValueError):
                self._failures.append(time.monotonic())
                raise IntakeError("unknown or already used code") from None
            if req.used:
                self._failures.append(time.monotonic())
                raise IntakeError("unknown or already used code")
            if dt.datetime.fromisoformat(req.expires) < _now():
                raise IntakeError("this code has expired; ask for a new one")
            if not hmac.compare_digest(req.name, name or ""):
                raise IntakeError(f"this code is for {req.name}, not {name}")
            check_value(name, value)
            self.store(req.human, name, value)
            req.used = True
            _write_private(path, json.dumps(req.__dict__))
            return req.human

    # ── stored secrets ───────────────────────────────────────────────────────

    def _secret_path(self, human, name):
        if not human.replace("-", "").replace("_", "").isalnum() or name not in NAMES:
            raise IntakeError("bad human or secret name")
        return os.path.join(self.secrets_dir, "humans", human, name)

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
    if not isinstance(value, str) or not value.strip():
        raise IntakeError("empty secret")
    if name == "claude-token" and (not value.startswith("sk-ant-") or len(value) < 40):
        raise IntakeError("that doesn't look like a Claude token")


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
