"""The lab registry: `lab.yaml`, the one description of the lab (DESIGN.md §8).

It lists the humans, agents, hosts and credentials. Every component reads it. It holds
credential *metadata* (name, host, expiry), never secret values.

`load()` parses and type-checks the file. `check()` validates the whole lab (references,
roles, channels, policies, expiring credentials) and returns every issue at once, so one
`lab check` run shows everything that's wrong.
"""
import datetime as dt
import re
from dataclasses import dataclass, field

import yaml

from ..policy import grants as G
from ..policy import sandbox

ROLES = ("worker", "manager", "secretary")
DEFAULT_WORKDIR = "~/work"
# Auto-compaction: managers hold long, chatty Slack conversations, so they compact about 5x
# earlier than Claude's default (which fires just below the full window); others keep it.
DEFAULT_COMPACT_PERCENT = {"manager": 17}
USER_PREFIX = "lab-"
# an agent id becomes part of its Unix user name: lab-<agent-id>, at most 32 characters
_AGENT_ID = re.compile(r"^[a-z][a-z0-9-]*$")
CHANNEL_TYPES = ("slack_dm",)   # first version: Slack DM only (DESIGN.md §7)
ACCESS = ("shared", "dedicated")
HOST_KINDS = ("onprem", "cloud")
POWER = ("always_on", "on_demand")
EXPIRY_WARN_DAYS = 14

# key -> (type, required). Types are checked; unknown keys are errors.
_TOP = {"lab": (str, True), "tailnet": (str, False), "trust_tailnet": (bool, False),
        "announcements": (str, False),     # Slack channel (id or #name) for lab-wide notices
        "humans": (dict, True),
        "agents": (dict, True), "hosts": (dict, True), "credentials": (list, False)}
_HUMAN = {"name": (str, False), "slack_user": (str, False), "channels": (list, False),
          "admin": (bool, False),           # may see agents' logs in the web UI
          "tailscale_login": (str, False)}  # how the web UI recognises them (tailscale whois)
_CHANNEL = {"type": (str, True), "timeout_s": (int, False), "on_timeout": (str, False)}
_AGENT = {"role": (str, True), "human": (str, True), "host": (str, True),
          "model": (str, False), "access": (str, False), "workdir": (str, False),
          "gpu": (bool, False), "port": (int, False),
          "display_name": (str, False), "compact_percent": (int, False),
          "grants": (list, False),
          "slack_app": (str, False), "max_parallel_gpu_tasks": (int, False)}
_HOST = {"kind": (str, True), "provider": (str, False), "instance": (str, False),
         "preemptible": (bool, False), "power": (str, False), "idle_stop_min": (int, False),
         "restart_by": (str, False), "tailnet_name": (str, False),
         "ssh_user": (str, False)}      # who `lab apply` logs in as (default: the admin)
_CRED = {"name": (str, True), "host": (str, True), "expires": ((dt.date, type(None)), True),
         "scope": (str, False), "agent": (str, False)}


@dataclass
class Issue:
    level: str      # "error" | "warning"
    where: str
    message: str

    def __str__(self):
        return f"{self.level.upper():7} {self.where}: {self.message}"


@dataclass
class Lab:
    path: str
    data: dict
    issues: list = field(default_factory=list)   # parse-time issues

    @property
    def name(self):
        return self.data.get("lab")

    @property
    def humans(self):
        return self.data.get("humans") or {}

    @property
    def agents(self):
        return self.data.get("agents") or {}

    @property
    def hosts(self):
        return self.data.get("hosts") or {}

    @property
    def credentials(self):
        return self.data.get("credentials") or []

    def address(self, agent_id):
        """Where the agent's service is reached on the tailnet: its host's tailnet name
        (`tailnet_name`, or `<host>.<tailnet>`) and its port."""
        a = self.agents[agent_id]
        host = self.hosts.get(a.get("host")) or {}
        name = host.get("tailnet_name") or f"{a.get('host')}.{self.data.get('tailnet')}"
        default = {"worker": 8770, "secretary": 8771}.get(a.get("role"), 8773)
        return name, a.get("port", default)

    def address_of_host(self, host_id):
        h = self.hosts.get(host_id) or {}
        return h.get("tailnet_name") or f"{host_id}.{self.data.get('tailnet')}"

    def worker_url(self, agent_id):
        name, port = self.address(agent_id)
        return f"http://{name}:{port}/mcp"

    def unix_user(self, agent_id):
        """The dedicated Unix user the agent runs as (DESIGN.md §10)."""
        return USER_PREFIX + agent_id

    def standing_grants(self, agent_id):
        """The agent's standing grants from lab.yaml (raises GrantError if malformed)."""
        return [G.from_mapping(m) for m in self.agents[agent_id].get("grants") or []]

    def agent_settings(self, agent_id, extra_grants=(), now=None):
        """The agent's Claude Code settings: the sandbox model + its grants (DESIGN.md §6)."""
        a = self.agents[agent_id]
        return sandbox.settings(
            workdir=a.get("workdir", DEFAULT_WORKDIR),
            tailnet=self.data.get("tailnet"),
            trust_tailnet=self.data.get("trust_tailnet", True),
            grant_list=self.standing_grants(agent_id) + list(extra_grants),
            now=now,
            gpu=a.get("gpu", a.get("role") == "worker"),
            compact_percent=a.get("compact_percent",
                                  DEFAULT_COMPACT_PERCENT.get(a.get("role"))))

    def display_name(self, agent_id):
        """How humans see the agent (Slack, web UI, how it introduces itself)."""
        return self.agents[agent_id].get("display_name") or agent_id


def _fields(obj, spec, where, issues):
    if not isinstance(obj, dict):
        issues.append(Issue("error", where, f"expected a mapping, got {type(obj).__name__}"))
        return False
    for k in obj:
        if k not in spec:
            issues.append(Issue("error", where, f"unknown key {k!r}"))
    for k, (typ, required) in spec.items():
        if k not in obj:
            if required:
                issues.append(Issue("error", where, f"missing required key {k!r}"))
            continue
        v = obj[k]
        # bool is a subclass of int; don't let `true` pass as an int
        if typ is int and isinstance(v, bool) or not isinstance(v, typ):
            names = typ.__name__ if isinstance(typ, type) else " or ".join(t.__name__ for t in typ)
            issues.append(Issue("error", f"{where}.{k}", f"expected {names}, got {v!r}"))
    return True


def load(path):
    with open(path) as f:
        data = yaml.safe_load(f)
    issues = []
    if not isinstance(data, dict):
        return Lab(path, {}, [Issue("error", path, "lab.yaml must be a mapping")])
    _fields(data, _TOP, "lab", issues)
    return Lab(path, data, issues)


def check(lab: Lab, today: dt.date | None = None):
    """Validate the whole lab. Returns a list of Issues (errors and warnings)."""
    today = today or dt.date.today()
    issues = list(lab.issues)
    E = lambda where, msg: issues.append(Issue("error", where, msg))      # noqa: E731
    W = lambda where, msg: issues.append(Issue("warning", where, msg))    # noqa: E731

    for hid, h in lab.humans.items():
        where = f"humans.{hid}"
        if not _fields(h or {}, _HUMAN, where, issues):
            continue
        for i, ch in enumerate((h or {}).get("channels") or []):
            cw = f"{where}.channels[{i}]"
            if not _fields(ch, _CHANNEL, cw, issues):
                continue
            if ch.get("type") not in CHANNEL_TYPES:
                E(cw, f"type must be one of {CHANNEL_TYPES}, got {ch.get('type')!r}")
            if "timeout_s" in ch and isinstance(ch["timeout_s"], int) and ch["timeout_s"] <= 0:
                E(cw, "timeout_s must be positive")
            if ch.get("on_timeout", "deny") != "deny":
                E(cw, "on_timeout must be 'deny' (a timeout never approves)")

    for hid, h in lab.hosts.items():
        where = f"hosts.{hid}"
        if not _fields(h or {}, _HOST, where, issues):
            continue
        if h.get("kind") not in HOST_KINDS:
            E(where, f"kind must be one of {HOST_KINDS}, got {h.get('kind')!r}")
        if h.get("kind") == "cloud" and not h.get("provider"):
            E(where, "a cloud host needs a provider")
        if "power" in h and h["power"] not in POWER:
            E(where, f"power must be one of {POWER}, got {h['power']!r}")
        rb = h.get("restart_by")
        if rb is not None and lab.agents.get(rb, {}).get("role") != "secretary":
            E(where, f"restart_by must name the secretary agent, got {rb!r}")

    roles = {}
    for aid, a in lab.agents.items():
        where = f"agents.{aid}"
        if not _fields(a or {}, _AGENT, where, issues):
            continue
        role = a.get("role")
        roles.setdefault(role, []).append(aid)
        if not _AGENT_ID.match(aid) or len(USER_PREFIX + aid) > 32:
            E(where, f"agent id must be lowercase letters, digits and '-', starting with a "
                     f"letter, at most {32 - len(USER_PREFIX)} characters: it becomes the "
                     f"Unix user {USER_PREFIX}{aid}")
        if role not in ROLES:
            E(where, f"role must be one of {ROLES}, got {role!r}")
        if a.get("human") not in lab.humans:
            E(where, f"human {a.get('human')!r} is not in humans "
                     "(every agent is attached to a human member, in terms of tokens)")
        if a.get("host") not in lab.hosts:
            E(where, f"host {a.get('host')!r} is not in hosts")
        if role in ("worker", "manager"):
            if a.get("access", "dedicated") not in ACCESS:
                E(where, f"access must be one of {ACCESS}, got {a.get('access')!r}")
        elif "access" in a:
            E(where, "'access' applies to workers and managers only")
        if role != "worker" and "max_parallel_gpu_tasks" in a:
            E(where, "'max_parallel_gpu_tasks' applies to workers only")
        if role != "manager" and "slack_app" in a:
            E(where, "'slack_app' applies to managers only")
        # (slack_app is informational: `lab slack-verify <manager> --write` fills it in)
        if role == "secretary":
            host = lab.hosts.get(a.get("host")) or {}
            if host.get("kind") == "cloud":
                W(where, "the secretary is meant to run on an always-on on-prem host")
        cp = a.get("compact_percent")
        if isinstance(cp, int) and not isinstance(cp, bool) and not 1 <= cp <= 100:
            E(where, "compact_percent must be between 1 and 100")
        workdir = a.get("workdir", DEFAULT_WORKDIR)
        if isinstance(workdir, str) and not workdir.startswith(("/", "~/")):
            E(where, f"workdir must be an absolute or ~/ path, got {workdir!r}")
        for j, m in enumerate(a.get("grants") or []):
            try:
                g = G.from_mapping(m)
            except G.GrantError as e:
                E(f"{where}.grants[{j}]", str(e))
                continue
            if g.expires is not None:
                E(f"{where}.grants[{j}]", "standing grants in lab.yaml don't expire; "
                                          "timed grants are given by a human through the broker")

    for hid, h in lab.humans.items():
        if (h or {}).get("admin") and not (h or {}).get("tailscale_login"):
            W(f"humans.{hid}", "an admin needs tailscale_login to see agents' logs in the web UI")
    if lab.data.get("trust_tailnet", True) and not lab.data.get("tailnet"):
        W("lab", "no tailnet set: sandboxed commands can't reach lab resources")

    secretaries = roles.get("secretary", [])
    if len(secretaries) != 1:
        E("agents", f"a lab has exactly one secretary, found {len(secretaries)}")
    for hid, h in lab.humans.items():
        mine = [aid for aid in roles.get("manager", []) if lab.agents[aid].get("human") == hid]
        if mine and not (h or {}).get("channels"):
            E(f"humans.{hid}", "has a manager but no permission channel")
        if mine and not (h or {}).get("slack_user"):
            E(f"humans.{hid}", "has a manager but no slack_user (permissions go to their Slack DM)")
        if len(mine) > 1:
            W(f"humans.{hid}", f"has {len(mine)} managers: {mine}")
    for aid in roles.get("worker", []):
        a = lab.agents[aid]
        if a.get("access", "dedicated") == "dedicated":
            owner = a.get("human")
            if not any(lab.agents[m].get("human") == owner for m in roles.get("manager", [])):
                W(f"agents.{aid}", f"dedicated to {owner!r}, who has no manager to submit tasks")

    for i, c in enumerate(lab.credentials):
        where = f"credentials[{i}]"
        if not _fields(c, _CRED, where, issues):
            continue
        where = f"credentials.{c.get('name', i)}"
        if c.get("host") not in lab.hosts:
            E(where, f"host {c.get('host')!r} is not in hosts")
        if c.get("agent") is not None and c["agent"] not in lab.agents:
            E(where, f"agent {c['agent']!r} is not in agents")
        exp = c.get("expires")
        if isinstance(exp, dt.date):
            left = (exp - today).days
            if left < 0:
                E(where, f"expired {-left} days ago ({exp})")
            elif left <= EXPIRY_WARN_DAYS:
                W(where, f"expires in {left} days ({exp})")
    return issues
