"""What an agent knows about its seat's usage, with when it learned it.

Two sources, both from the Agent SDK's message stream:
  - RateLimitEvent: the seat's limit windows (5-hour, weekly...), utilization and reset
    time. Emitted only when a window's status changes, so each reading is stamped with
    when it was observed: the web UI shows "as of ..." next to it.
  - ResultMessage.model_usage: tokens per model for each run (subagents included).
    Dollar figures in it are list-price estimates and are not kept.

Kept in one small JSON file; served by the agent's health endpoint; collected by the
secretary.
"""
import datetime as dt
import json
import os
import tempfile
import threading

TOKEN_FIELDS = ("inputTokens", "outputTokens", "cacheReadInputTokens",
                "cacheCreationInputTokens")
KEEP_DAYS = 14


def _now():
    return dt.datetime.now(dt.timezone.utc)


class UsageLog:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {"limits": {}, "days": {}, "tasks": {}}

    def _save(self, data):
        d = os.path.dirname(self.path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)

    def rate_limit(self, info):
        """Record a RateLimitInfo (or a dict with the same fields)."""
        get = (lambda k: info.get(k)) if isinstance(info, dict) else (lambda k: getattr(info, k, None))
        kind = get("rate_limit_type") or "unknown"
        resets = get("resets_at")
        with self._lock:
            data = self.load()
            data["limits"][kind] = {
                "status": get("status"),
                "utilization": get("utilization"),
                "resets_at": (dt.datetime.fromtimestamp(resets, dt.timezone.utc).isoformat()
                              if resets else None),
                "observed_at": _now().isoformat(timespec="seconds"),
            }
            self._save(data)

    def run(self, task_id, model_usage):
        """Record one run's tokens (ResultMessage.model_usage: {model: {...Tokens: n}})."""
        tokens = {}
        for model, u in (model_usage or {}).items():
            u = u if isinstance(u, dict) else getattr(u, "__dict__", {})
            tokens[model] = {k: int(u.get(k) or 0) for k in TOKEN_FIELDS}
        if not tokens:
            return
        day = _now().date().isoformat()
        with self._lock:
            data = self.load()
            for bucket in (data["days"].setdefault(day, {}),
                           data["tasks"].setdefault(task_id or "-", {})):
                for model, t in tokens.items():
                    acc = bucket.setdefault(model, dict.fromkeys(TOKEN_FIELDS, 0))
                    for k, v in t.items():
                        acc[k] += v
            cutoff = (_now().date() - dt.timedelta(days=KEEP_DAYS)).isoformat()
            data["days"] = {d: v for d, v in data["days"].items() if d >= cutoff}
            data["updated_at"] = _now().isoformat(timespec="seconds")
            self._save(data)

    def limit_hit(self, message):
        """An agent ran into a limit (the error text says which and when it resets)."""
        with self._lock:
            data = self.load()
            data["last_limit_error"] = {"message": message[:300],
                                        "observed_at": _now().isoformat(timespec="seconds")}
            self._save(data)
