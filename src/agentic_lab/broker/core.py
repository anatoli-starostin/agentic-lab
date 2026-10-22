"""The approval broker (DESIGN.md §6.3): code, not an LLM, decides who answered.

A worker that needs something outside its sandbox files a request here. The broker asks the
human (through a channel: Slack DM), and records the decision only when it comes from that
human's own verified interaction. A request waits for its human's answer; if the human set
a timeout (`timeout_s`), an unanswered request is denied when it runs out. Workers pull
decisions from the broker; nothing else can hand them one.

Decisions:
    once    this one action runs (e.g. one command outside the sandbox)
    grant   widen the agent's sandbox for a while: a domain, a writable folder or a tool,
            for 1 hour or for the rest of the task. Grants are stored here, and workers
            fetch their active grants when a session starts.
    deny    with an optional note from the human ("do it differently")
    timeout nobody answered in time (only with a timeout set)
    withdrawn the agent stopped waiting (e.g. its task was cancelled)
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

from ..policy import grants as G

PENDING, DECIDED = "pending", "decided"
ACTIONS = ("once", "hour", "task", "deny")
TASK_GRANT_MAX = dt.timedelta(hours=24)      # a "this task" grant never outlives a day
DEFAULT_TIMEOUT_S = None        # no timeout: a request waits for its human (lab.yaml: timeout_s)


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
    context: str | None = None      # a manager's note for the human
    channel_ref: dict = field(default_factory=dict)   # e.g. the Slack message, to update it
    manager: str | None = None      # the manager whose task (or own action) this is
    requester_confirmed: bool = False   # the human came from a manager's task registration

    def view(self):
        return asdict(self)


# ── what a request could be granted ──────────────────────────────────────────

_URL = re.compile(r"\bhttps?://[^\s'\"<>]+")
_VIOLATION_HOST = re.compile(r"network(?:-outbound)?\s+([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?::\d+)?")


# approved one at a time, never granted for a while: a long run is outside the sandbox
ONE_OFF_ONLY = ("mcp__lab__start_run",)


def grant_options(tool, tool_input, violations):
    """The grants a human could give instead of a one-off approval."""
    opts = []
    if tool in ONE_OFF_ONLY:
        return opts

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
        if folder.startswith(("/", "~/")) and not G.write_refused(folder):
            add("write", folder)            # only what a write grant may name at all
    elif tool in ("WebFetch", "WebSearch") or (tool.startswith("mcp__") and
                                                 G.grantable_tool(tool)):
        add("tool", tool)
    return opts


MAX_DETAIL = 20000          # characters shown; a longer request can only be denied
TRUNCATED = "⚠ TRUNCATED"   # marks a detail that isn't shown in full


# characters that print as nothing (or as a blank) though they're "printable"
BLANKS = {0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800, 0x034F, 0x17B4, 0x17B5, 0x180E, 0x2060,
          0x3000, 0x00A0, 0x00AD, 0xFEFF, 0x061C, 0x200B, 0x200C, 0x200D, 0x11A3, 0x11A4,
          0x11A5, 0x11A6, 0x11A7, 0x1D159, 0x1D173, 0x1D174, 0x1D175, 0x1D176, 0x1D177,
          0x1D178, 0x1D179, 0x1D17A, 0xFFFC, 0xFFFD, 0x2028, 0x2029, 0x202F, 0x205F}


def _shown(c, ascii_only):
    import unicodedata
    o = ord(c)
    if c == "\n" or 0x20 <= o < 0x7F:
        return c
    if ascii_only or o in BLANKS or unicodedata.category(c)[0] in "CMZ":
        return f"\\x{o:02x}" if o < 0x80 else f"\\u{{{o:x}}}"   # \u{…}: never ambiguous
    return c


def _non_ascii_note(text):
    """A line before file content that holds non-ASCII letters (which can look like ASCII
    ones): how many, and the first few, escaped."""
    odd = [c for c in text if ord(c) >= 0x80]
    if not odd:
        return ""
    first = " ".join(f"\\u{{{ord(c):x}}}" for c in dict.fromkeys(odd[:200]))
    return (f"⚠ the content has {len(odd)} non-ASCII characters (look-alikes of ASCII "
            f"letters among them?): {first[:200]}\n")


def visible(text, ascii_only=False):
    """Text made reviewable: runs of spaces or tabs shown with their count, long runs of
    blank lines counted, and every character that could hide something (controls, marks,
    blanks and fillers that print as nothing) as an escape. `ascii_only`: everything
    outside ASCII escaped (commands, paths, addresses)."""
    import re as _re
    text = "".join(_shown(c, ascii_only) for c in text)     # first: the markers stay as is
    text = _re.sub(r" {8,}", lambda m: f" ⟨{len(m.group())} blanks⟩ ", text)
    return _re.sub(r"\n{4,}", lambda m: f"\n⟨{len(m.group())} line breaks⟩\n", text)


def _full(text, ascii_only=False):
    text = visible(text, ascii_only)
    if len(text) > MAX_DETAIL:
        return (text[:MAX_DETAIL]
                + f"\n{TRUNCATED}: {len(text) - MAX_DETAIL} more characters not shown")
    return text


def describe(tool, tool_input):
    """What the agent wants to do, in full (nothing approved is hidden)."""
    if tool == "Bash":
        return _full(tool_input.get("command", ""), ascii_only=True)
    path = visible(str(tool_input.get("file_path") or tool_input.get("notebook_path")),
                   ascii_only=True)
    if tool == "Write":
        content = str(tool_input.get("content", ""))
        return _full(f"Write {path}:\n{_non_ascii_note(content)}{content}")
    if tool == "Edit":
        new = str(tool_input.get("new_string", ""))
        return _full(f"Edit {path}:\n{_non_ascii_note(new)}--- replace:\n"
                     f"{tool_input.get('old_string', '')}\n--- with:\n{new}")
    if tool in ("MultiEdit", "NotebookEdit"):
        return _full(f"{tool} {path}:\n{json.dumps(tool_input, indent=1)}")
    if tool == "WebFetch":
        return _full(f"fetch {tool_input.get('url', '')}", ascii_only=True)
    if tool == "WebSearch":
        return _full(f"search: {tool_input.get('query', '')}")
    if tool == "mcp__lab__start_run":
        return _full(f"start a long run OUTSIDE the sandbox, as the agent, until it ends or "
                     f"is stopped ({tool_input.get('name', 'run')})"
                     + (f" [in {tool_input['cwd']}]" if tool_input.get("cwd") else "")
                     + f":\n{tool_input.get('command', '')}", ascii_only=True)
    return _full(f"{tool} {json.dumps(tool_input, indent=1)}")


# ── the broker ───────────────────────────────────────────────────────────────

class Broker:
    """Requests and grants as files under `root`. `humans` maps a human id to
    {"slack_user": ..., "timeout_s": ...}; `agents` maps an agent id to its lab.yaml entry
    (role, human, access). Both come from lab.yaml."""

    def __init__(self, root, humans, agents=None):
        self.root = root
        self.humans = humans
        self.agents = agents or {}
        self.req_dir = os.path.join(root, "requests")
        self.grant_dir = os.path.join(root, "grants")
        self.task_dir = os.path.join(root, "tasks")
        for d in (self.req_dir, self.grant_dir, self.task_dir):
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

    # tasks managers registered: who really asked for them
    def _task_path(self, task_id):
        if not task_id.startswith("t-") or not task_id[2:].isalnum():
            raise BrokerError(f"bad task id {task_id!r}")
        return os.path.join(self.task_dir, task_id + ".json")

    def register_task(self, manager, worker, task_id, requester):
        """A manager records who asked for a task it submitted. A worker's later requests
        for that task then go to this human, whatever the worker claims."""
        if requester not in self.humans:
            raise BrokerError(f"unknown human {requester!r}")
        self.check_access(manager, requester)
        with self._lock:
            path = self._task_path(task_id)
            if os.path.exists(path):
                raise BrokerError(f"task {task_id} is already registered")
            self._write(path, {"manager": manager, "worker": worker, "requester": requester})

    def registered_task(self, task_id):
        try:
            with open(self._task_path(task_id)) as f:
                return json.load(f)
        except (FileNotFoundError, BrokerError):
            return None

    def check_access(self, agent, human):
        """May `human` use `agent`? A dedicated agent serves only its own human."""
        a = self.agents.get(agent)
        if a is None:
            raise BrokerError(f"unknown agent {agent!r}")
        if a.get("access", "dedicated") == "dedicated" and a.get("human") != human:
            raise BrokerError(f"{agent} is dedicated to {a.get('human')}, not {human}")

    # requests
    def submit(self, agent, human, tool, tool_input, task_id=None, violations=(),
               role="worker") -> Request:
        """File a request. For a worker's task registered by a manager, the registered
        requester is asked (the worker's claim only counts for unregistered tasks). A
        manager's own request goes to the member it names, if they may use that manager."""
        manager, confirmed = None, False
        if role == "manager":
            manager = agent
            if human not in self.humans:
                raise BrokerError(f"unknown human {human!r}")
            self.check_access(agent, human)
            confirmed = True
        elif task_id:
            reg = self.registered_task(task_id)
            if reg is not None:
                if reg["worker"] != agent:
                    raise BrokerError(f"task {task_id} isn't {agent}'s")
                human, manager, confirmed = reg["requester"], reg["manager"], True
        if human not in self.humans:
            raise BrokerError(f"unknown human {human!r}")
        timeout = self.humans[human].get("timeout_s") or DEFAULT_TIMEOUT_S   # None: no timeout
        t = now()
        req = Request(
            id=f"r-{t.strftime('%Y%m%d%H%M%S%f')}{secrets.token_hex(3)}", agent=agent,
            task_id=task_id, human=human, tool=tool, detail=describe(tool, tool_input),
            violations=list(violations or [])[-5:],
            options=grant_options(tool, tool_input, violations),
            created=_iso(t),
            expires=_iso(t + dt.timedelta(seconds=int(timeout))) if timeout else None,
            manager=manager, requester_confirmed=confirmed)
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
            if action != "deny" and TRUNCATED in req.detail:
                raise BrokerError("not shown in full, so it can only be denied")
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
                if req.expires and now() >= dt.datetime.fromisoformat(req.expires):
                    req.state = DECIDED
                    req.decision = {"action": "timeout", "by": None, "at": _iso(now()),
                                    "note": None}
                    self._save(req)
                    out.append(req)
        for req in out:
            self._notify(req)
        return out

    def withdraw(self, rid, agent):
        """The agent that filed a request stops waiting for it (its task was cancelled):
        it's closed, so nobody is asked about it any more."""
        with self._lock:
            req = self.get(rid)
            if req.agent != agent:
                raise BrokerError(f"no request {rid!r}")
            if req.state != PENDING:
                return req
            req.state = DECIDED
            req.decision = {"action": "withdrawn", "by": None, "at": _iso(now()), "note": None}
            self._save(req)
        self._notify(req)
        return req

    def for_manager(self, manager):
        """Pending requests a manager may see: those of its tasks and its own."""
        return [r for r in self.pending() if r.manager == manager or r.agent == manager]

    def add_context(self, rid, text, manager=None):
        """A manager's note shown to the human with the request (it can't decide)."""
        with self._lock:
            req = self.get(rid)
            if manager is not None and req.manager != manager:
                raise BrokerError(f"no request {rid!r}")
            if req.state != PENDING:
                raise BrokerError("already decided")
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
