"""An agent's own record of what it did, for the admin's log viewer (DESIGN.md §9).

Each agent's service writes it while the agent works: messages in, replies out, tool calls
with a shortened input and result, approvals asked and decided, task events. One JSONL file
per day, kept for KEEP_DAYS. It's the lab's own format (Claude Code's internal transcript
format can change between versions), and it lives in the service's state folder, which the
agent itself can't read.
"""
import datetime as dt
import glob
import json
import os
import threading
import time

KEEP_DAYS = 14
MAX_TEXT = 2000


def _now():
    return dt.datetime.now(dt.timezone.utc)


def short(x, n=MAX_TEXT):
    s = x if isinstance(x, str) else json.dumps(x, default=str)
    return s if len(s) <= n else s[:n] + f"… [{len(s) - n} more characters]"


class TurnClock:
    """Where a conversation turn's time goes (#69): Claude Code's start-up (until its init
    message), the first reply, the whole turn. Logged as a `timing` event."""

    def __init__(self, now=time.monotonic):
        self.now, self.t0 = now, now()
        self.startup = self.first = None

    def started(self):
        if self.startup is None:
            self.startup = self.now() - self.t0

    def replied(self):
        if self.first is None:
            self.first = self.now() - self.t0

    def done(self):
        """(text, meta) for the event log."""
        total = self.now() - self.t0
        parts = [("start-up", self.startup), ("first reply", self.first), ("total", total)]
        text = ", ".join(f"{name} {s:.1f} s" for name, s in parts if s is not None)
        return text, {"startup_s": _secs(self.startup), "first_reply_s": _secs(self.first),
                      "total_s": _secs(total)}


def _secs(s):
    return None if s is None else round(s, 2)


class EventLog:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, mode=0o700, exist_ok=True)
        self._lock = threading.Lock()

    def add(self, kind, text="", **meta):
        """kind: message | reply | tool | result | approval | task | error | note | timing"""
        t = _now()
        rec = {"at": t.isoformat(timespec="seconds"), "kind": kind, "text": short(text or ""),
               **{k: v for k, v in meta.items() if v is not None}}
        path = os.path.join(self.root, f"events-{t.date().isoformat()}.jsonl")
        with self._lock:
            with open(path, "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
            os.chmod(path, 0o600)
        return rec

    def read(self, since=None, limit=500):
        """Events newer than `since` (ISO time, or a timedelta ago), newest last."""
        if isinstance(since, dt.timedelta):
            since = (_now() - since).isoformat(timespec="seconds")
        files = sorted(glob.glob(os.path.join(self.root, "events-*.jsonl")))
        if since:
            day = since[:10]
            files = [f for f in files if os.path.basename(f)[7:17] >= day]
        out = []
        for path in files:
            with open(path) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not since or rec.get("at", "") > since:
                        out.append(rec)
        return out[-limit:]

    def prune(self):
        cutoff = (_now().date() - dt.timedelta(days=KEEP_DAYS)).isoformat()
        for path in glob.glob(os.path.join(self.root, "events-*.jsonl")):
            if os.path.basename(path)[7:17] < cutoff:
                os.remove(path)


def tool_summary(name, tool_input):
    """A tool call in one line: the command, the file, the URL..."""
    i = tool_input or {}
    for key in ("command", "file_path", "url", "query", "path", "pattern", "instruction"):
        if key in i:
            return f"{name}: {short(str(i[key]), 500)}"
    return f"{name}: {short(i, 300)}"
