"""The approval broker (DESIGN.md §6.3): code, not an LLM, decides who answered.

A worker that needs something outside its sandbox files a request here. The broker asks the
human (through a channel: Slack DM), and records the decision only when it comes from that
human's own verified interaction. Unanswered requests are denied on timeout. Workers pull
decisions from the broker; nothing else can hand them one.

Decisions:
    once    this one action runs (e.g. one command outside the sandbox)
    grant   widen the agent's sandbox for a while: a domain, a writable folder or a tool,
            for 1 hour or for the rest of the task. Grants are stored here, and workers
            fetch their active grants when a session starts.
    deny    with an optional note from the human ("do it differently")
    timeout nobody answered in time
"""
import datetime as dt
import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

PENDING, DECIDED = "pending", "decided"
ACTIONS = ("once", "hour", "task", "deny")
TASK_GRANT_MAX = dt.timedelta(hours=24)      # a "this task" grant never outlives a day
DEFAULT_TIMEOUT_S = 120


class BrokerError(ValueError):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.isoformat(timespec="seconds")


@dataclass
class Request:
    id: str
    agent: str              # the worker asking (authenticated by its token)
    task_id: str | None
    human: str              # who decides: the task's requester
    tool: str
    detail: str             # what will happen: the command, the path, the URL...
    violations: list        # what the sandbox refused just before ("network example.com:443")
    options: list           # grants that could cover it: [{"kind": "domain", "value": ...}]
    created: str
    expires: str
    state: str = PENDING
    decision: dict | None = None
    context: str | None = None      # a manager's note for the human (M3)
    channel_ref: dict = field(default_factory=dict)   # e.g. the Slack message, to update it

    def view(self):
        return asdict(self)


# ── what a request could be granted ──────────────────────────────────────────

_URL = re.compile(r"\bhttps?://[^\s'\"<>]+")
_VIOLATION_HOST = re.compile(r"network(?:-outbound)?\s+([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?::\d+)?")


def grant_options(tool, tool_input, violations):
    """The grants a human could give instead of a one-off approval."""
    opts = []

    def add(kind, value):
        o = {"kind": kind, "value": value}
        if value and o not in opts:
            opts.append(o)

    for v in violations or []:
        m = _VIOLATION_HOST.search(v)
        if m:
            add("domain", m.group(1).lower())
    if tool == "Bash":
        for url in _URL.findall(tool_input.get("command", "")):
            host = urlparse(url).hostname
            if host and not host.replace(".", "").isdigit():
                add("domain", host.lower())
    elif tool in ("Write", "Edit", "NotebookEdit"):
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        folder = os.path.dirname(path)
        if folder not in ("", "/") and folder != os.path.expanduser("~"):
            add("write", folder)
    elif tool in ("WebFetch", "WebSearch") or tool.startswith("mcp__"):
        add("tool", tool)
    return opts


def describe(tool, tool_input):
    """One line saying what the agent wants to do."""
    if tool == "Bash":
        return tool_input.get("command", "")[:2000]
    if tool in ("Write", "Edit", "NotebookEdit"):
        return f"{tool} {tool_input.get('file_path') or tool_input.get('notebook_path')}"
    if tool == "WebFetch":
        return f"fetch {tool_input.get('url', '')}"
    if tool == "WebSearch":
        return f"search: {tool_input.get('query', '')}"
    return f"{tool} {json.dumps(tool_input)[:500]}"


# ── the broker ───────────────────────────────────────────────────────────────

class Broker:
    """Requests and grants as files under `root`. `humans` maps a human id to
    {"slack_user": ..., "timeout_s": ...} (from lab.yaml)."""

    def __init__(self, root, humans):
        self.root = root
        self.humans = humans
        self.req_dir = os.path.join(root, "requests")
        self.grant_dir = os.path.join(root, "grants")
        for d in (self.req_dir, self.grant_dir):
            os.makedirs(d, mode=0o700, exist_ok=True)
        self._lock = threading.RLock()
        self.listeners = []         # callables(request) told about new/decided requests

    # storage
    def _write(self, path, data):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)

    def _req_path(self, rid):
        if not rid.startswith("r-") or not rid[2:].isalnum():
            raise BrokerError(f"bad request id {rid!r}")
        return os.path.join(self.req_dir, rid + ".json")

    def get(self, rid) -> Request:
        try:
            with open(self._req_path(rid)) as f:
                return Request(**json.load(f))
        except FileNotFoundError:
            raise BrokerError(f"no request {rid!r}") from None

    def _save(self, req):
        self._write(self._req_path(req.id), req.view())

    def pending(self, human=None):
        out = []
        for name in sorted(os.listdir(self.req_dir)):
            if name.startswith("r-") and name.endswith(".json"):
                r = self.get(name[:-5])
                if r.state == PENDING and (human is None or r.human == human):
                    out.append(r)
        return out

    def _notify(self, req):
        for fn in self.listeners:
            fn(req)

    # requests
    def submit(self, agent, human, tool, tool_input, task_id=None, violations=()) -> Request:
        if human not in self.humans:
            raise BrokerError(f"unknown human {human!r}")
        timeout = int(self.humans[human].get("timeout_s") or DEFAULT_TIMEOUT_S)
        t = now()
        req = Request(
            id=f"r-{t.strftime('%Y%m%d%H%M%S%f')}{secrets.token_hex(3)}", agent=agent,
            task_id=task_id, human=human, tool=tool, detail=describe(tool, tool_input),
            violations=list(violations or [])[-5:],
            options=grant_options(tool, tool_input, violations),
            created=_iso(t), expires=_iso(t + dt.timedelta(seconds=timeout)))
        with self._lock:
            self._save(req)
        self._notify(req)
        return req

    def decide(self, rid, action, by_slack_user, note=None) -> Request:
        """Record a human's decision. `by_slack_user` must be the request's human: the
        channel passes the verified identity of whoever clicked or replied."""
        if action not in ACTIONS:
            raise BrokerError(f"unknown action {action!r}")
        with self._lock:
            req = self.get(rid)
            if req.state != PENDING:
                raise BrokerError("already decided")
            expected = self.humans.get(req.human, {}).get("slack_user")
            if not expected or by_slack_user != expected:
                raise BrokerError("only the person asked can decide this request")
            if action in ("hour", "task") and not req.options:
                raise BrokerError("nothing to grant for this request")
            t = now()
            decision = {"action": action, "by": req.human, "at": _iso(t), "note": note}
            if action in ("hour", "task"):
                until = t + (dt.timedelta(hours=1) if action == "hour" else TASK_GRANT_MAX)
                grants = [{**o, "expires": _iso(until), "granted_by": req.human,
                           "task_id": req.task_id if action == "task" else None,
                           "request": req.id} for o in req.options]
                self._add_grants(req.agent, grants)
                decision["grants"] = grants
            req.state, req.decision = DECIDED, decision
            self._save(req)
        self._notify(req)
        return req

    def expire(self):
        """Deny every pending request past its deadline. Returns those it expired."""
        out = []
        with self._lock:
            for req in self.pending():
                if now() >= dt.datetime.fromisoformat(req.expires):
                    req.state = DECIDED
                    req.decision = {"action": "timeout", "by": None, "at": _iso(now()),
                                    "note": None}
                    self._save(req)
                    out.append(req)
        for req in out:
            self._notify(req)
        return out

    def add_context(self, rid, text):
        """A manager's note shown to the human with the request (it can't decide)."""
        with self._lock:
            req = self.get(rid)
            req.context = text[:1000]
            self._save(req)
        self._notify(req)
        return req

    def set_channel_ref(self, rid, ref):
        with self._lock:
            req = self.get(rid)
            req.channel_ref = ref
            self._save(req)

    # grants
    def _grant_path(self, agent):
        if not re.fullmatch(r"[a-z][a-z0-9-]*", agent):
            raise BrokerError(f"bad agent id {agent!r}")
        return os.path.join(self.grant_dir, agent + ".json")

    def _add_grants(self, agent, grants):
        current = self.grants(agent)
        self._write(self._grant_path(agent), current + grants)

    def grants(self, agent, task_done=()):
        """The agent's active grants (expired ones and those of finished tasks dropped)."""
        try:
            with open(self._grant_path(agent)) as f:
                all_grants = json.load(f)
        except FileNotFoundError:
            return []
        t = now()
        return [g for g in all_grants if dt.datetime.fromisoformat(g["expires"]) > t
                and not (g.get("task_id") and g["task_id"] in task_done)]
