"""What an agent knows about its seat's usage, with when it learned it.

Three sources:
  - RateLimitEvent (Agent SDK): the seat's limit windows (5-hour, weekly...), status and
    reset time. Emitted only when a window's status changes, and with a percentage only
    near the limit, so each reading is stamped with when it was observed: the web UI shows
    "as of ..." next to it.
  - The probe (`probe_loop`): every API reply carries the seat's percentages in headers
    (`anthropic-ratelimit-unified-5h-utilization`...), which Claude Code doesn't pass on.
    So every PROBE_EVERY, while the seat's 5-hour window is open, the service sends one
    tiny request (Haiku, 1 output token) and reads them: the numbers the Claude app shows.
    It doesn't probe on its own when no window is known to be open, so it never starts one;
    a human's "check now" (the web UI) probes regardless.
  - ResultMessage.model_usage: tokens per model for each run (subagents included).
    Dollar figures in it are list-price estimates and are not kept.

Kept in one small JSON file; served by the agent's health endpoint; collected by the
secretary.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import tempfile
import threading
import urllib.error
import urllib.request

log = logging.getLogger("agentic_lab.usage")

PROBE_EVERY = dt.timedelta(minutes=10)
PROBE_MODEL = "claude-haiku-5-5"
# a seat token (`claude setup-token`) is accepted only for Claude Code's own requests
PROBE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
WINDOW_HEADERS = {"5h": "five_hour", "7d": "seven_day"}

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
        reading = {
            "status": get("status"),
            "utilization": get("utilization"),
            "resets_at": (dt.datetime.fromtimestamp(resets, dt.timezone.utc).isoformat()
                          if resets else None),
            "observed_at": _now().isoformat(timespec="seconds"),
            "raw": get("raw"),              # everything the CLI sent, for later fields
        }
        with self._lock:
            data = self.load()
            data["limits"][kind] = _keep_percentage(data["limits"].get(kind), reading)
            self._save(data)

    def probed(self, readings):
        """Record a probe's readings: {kind: {status, utilization, resets_at}}."""
        at = _now().isoformat(timespec="seconds")
        with self._lock:
            data = self.load()
            for kind, r in readings.items():
                data["limits"][kind] = {**r, "observed_at": at, "source": "probe"}
            data["probed_at"] = at
            self._save(data)

    def should_probe(self, now=None):
        """Due, and the seat's 5-hour window known to be open: the agent worked since the
        last probe, or a reading says the window hasn't reset yet."""
        now = now or _now()
        data = self.load()
        last = data.get("probed_at")
        if last and now - dt.datetime.fromisoformat(last) < PROBE_EVERY:
            return False
        worked = data.get("updated_at") and (not last or data["updated_at"] > last)
        resets = (data["limits"].get("five_hour") or {}).get("resets_at")
        return bool(worked or (resets and dt.datetime.fromisoformat(resets) > now))

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


def _keep_percentage(old, new):
    """A status-only reading for the same window keeps the last measured percentage,
    stamped with when it was measured."""
    if (new.get("utilization") is None and old and old.get("utilization") is not None
            and old.get("resets_at") == new.get("resets_at")):
        return {**new, "utilization": old["utilization"],
                "utilization_at": old.get("utilization_at") or old.get("observed_at")}
    return new


def parse_headers(headers):
    """{kind: {status, utilization, resets_at}} from a reply's rate-limit headers."""
    h = {k.lower(): v for k, v in headers.items()}
    out = {}
    for short, kind in WINDOW_HEADERS.items():
        pre = f"anthropic-ratelimit-unified-{short}-"
        if pre + "utilization" not in h and pre + "status" not in h:
            continue
        try:
            util = float(h[pre + "utilization"]) if pre + "utilization" in h else None
            reset = int(h[pre + "reset"]) if pre + "reset" in h else None
        except ValueError:
            continue
        out[kind] = {"status": h.get(pre + "status"), "utilization": util,
                     "resets_at": (dt.datetime.fromtimestamp(reset, dt.timezone.utc).isoformat()
                                   if reset else None)}
    return out


def probe(seat, timeout=30):
    """The seat's limits, read from the reply to the smallest possible request."""
    body = json.dumps({"model": PROBE_MODEL, "max_tokens": 1, "system": PROBE_SYSTEM,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "Authorization": f"Bearer {seat}", "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return parse_headers(r.headers)
    except urllib.error.HTTPError as e:
        found = parse_headers(e.headers)     # a 429 at the limit still says where we are
        if found:
            return found
        raise


async def probe_now(usage, seat):
    """Probe once and record it; returns the readings (or raises)."""
    found = await asyncio.to_thread(probe, seat)
    usage.probed(found)
    return found


async def probe_loop(usage, seat, stop, every=60):
    """Beside an agent's service: probe whenever `usage.should_probe()`."""
    while not stop.is_set():
        if seat and usage.should_probe():
            try:
                await probe_now(usage, seat)
            except Exception as e:
                log.warning("usage probe failed: %s", e)
                usage.probed({})            # don't retry before PROBE_EVERY
        try:
            await asyncio.wait_for(stop.wait(), every)
        except asyncio.TimeoutError:
            pass
