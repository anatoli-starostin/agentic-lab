"""What an agent is doing right now, for the web UI's live view of its log.

`Live` follows the Claude turns going on in this process: since when, which tools are
running (and since when), the latest text, and tokens. `processes()` lists the agent's
Unix user's processes (a stray background command, a run's job). Both are read only when
someone looks (the status endpoint's `now`, the worker's `now` tool), never logged.
"""
import datetime as dt
import os
import re
import subprocess
import threading

TEXT_CHARS = 300
ARGS_CHARS = 200
MAX_PROCESSES = 20

# what a command line may carry that the page shouldn't show: key=..., --token ..., and
# anything that looks like a credential (long runs of key characters, known prefixes)
_SECRETISH = [
    re.compile(r"(?i)((?:key|token|secret|passw(?:or)?d|auth)[\w-]*(?:=|:|\s+))(\S+)"),
    re.compile(r"\b(?:gh[pousr]_|github_pat_|sk-|xox[abp]-|tskey-)[\w-]+"),
    re.compile(r"\b[A-Za-z0-9+/_-]{40,}\b"),
]


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.isoformat(timespec="seconds")


def mask(text):
    for i, rx in enumerate(_SECRETISH):
        text = rx.sub((lambda m: m.group(1) + "•••") if i == 0 else (lambda m: "•••"), text)
    return text


class Live:
    """The turns in progress, by key (a conversation, a task)."""

    def __init__(self):
        self._turns = {}
        self._lock = threading.Lock()

    def start(self, key, **meta):
        with self._lock:
            self._turns[key] = {**meta, "key": key, "started_at": _iso(_now()), "tools": {},
                                "tool_calls": 0, "text": None, "output_tokens": 0,
                                "context_tokens": None}

    def end(self, key):
        with self._lock:
            self._turns.pop(key, None)

    def _turn(self, key):
        return self._turns.get(key)

    def assistant(self, key, msg, summary):
        """An AssistantMessage: its text, its tool calls (`summary(name, input)`), usage."""
        with self._lock:
            t = self._turn(key)
            if t is None:
                return
            for b in getattr(msg, "content", None) or []:
                if hasattr(b, "text") and isinstance(b.text, str):
                    t["text"] = b.text[-TEXT_CHARS:]
                elif hasattr(b, "id") and hasattr(b, "name"):
                    t["tools"][b.id] = {"tool": summary(b.name, b.input),
                                        "since": _iso(_now())}
                    t["tool_calls"] += 1
            u = getattr(msg, "usage", None) or {}
            if u:
                t["output_tokens"] += u.get("output_tokens") or 0
                t["context_tokens"] = sum(u.get(k) or 0 for k in (
                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))

    def tool_done(self, key, tool_use_id):
        with self._lock:
            t = self._turn(key)
            if t is not None:
                t["tools"].pop(tool_use_id, None)

    def snapshot(self):
        with self._lock:
            return [{**t, "tools": list(t["tools"].values())} for t in self._turns.values()]


def processes(uid=None):
    """The processes of this Unix user (default: ours), busiest first, command lines
    shortened and masked. ps itself and this process are left out."""
    uid = os.getuid() if uid is None else uid
    try:
        out = subprocess.run(["ps", "-ww", "-U", str(uid), "-o", "pid=,etimes=,pcpu=,rss=,args="],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    procs = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, etimes, pcpu, rss, args = parts
        if int(pid) == os.getpid() or args.startswith("ps -ww -U"):
            continue
        procs.append({"pid": int(pid), "seconds": int(etimes), "cpu": float(pcpu),
                      "mem_mb": round(int(rss) / 1024), "command": mask(args)[:ARGS_CHARS]})
    procs.sort(key=lambda p: (-p["cpu"], -p["seconds"]))
    return procs[:MAX_PROCESSES]
