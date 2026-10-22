"""Grants: permissions customised on the fly (DESIGN.md §6.2).

A grant widens what an agent may do without asking:

    domain   a host its sandboxed commands may reach      {domain: arxiv.org}
    write    a folder its sandbox (and file tools) may write {write: ~/datasets}
    tool     a tool it may use without asking              {tool: WebFetch}

A grant is either standing (in lab.yaml, no expiry) or given by a human through the broker
("arxiv.org for 1 hour"). A time-limited grant always widens the SANDBOX. It never means
"trust unsandboxed commands for a while", because an unsandboxed command can do anything.
"""
import datetime as dt
import os
import re
from dataclasses import dataclass

KINDS = ("domain", "write", "tool")

_DOMAIN = re.compile(r"^(\*\.)?([a-z0-9-]+\.)+[a-z0-9-]+(:\d+)?$")
# a tool name, or an MCP tool glob anchored to its server: mcp__server__* / mcp__server__get_*
_TOOL = re.compile(r"^([A-Z][A-Za-z]+|mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_*-]+)$")


class GrantError(ValueError):
    pass


@dataclass(frozen=True)
class Grant:
    kind: str
    value: str
    expires: dt.datetime | None = None    # None = standing
    granted_by: str | None = None         # the human (None for standing grants in lab.yaml)
    reason: str | None = None

    def active(self, now: dt.datetime) -> bool:
        return self.expires is None or now < self.expires

    def __str__(self):
        until = f" until {self.expires:%Y-%m-%d %H:%M}" if self.expires else ""
        return f"{self.kind} {self.value}{until}"


def validate(kind, value):
    """Raise GrantError if `value` is not a sensible grant of `kind`."""
    if kind not in KINDS:
        raise GrantError(f"grant kind must be one of {KINDS}, got {kind!r}")
    if not isinstance(value, str) or not value:
        raise GrantError(f"{kind} grant needs a non-empty string")
    if kind == "domain" and not _DOMAIN.match(value):
        raise GrantError(f"not a domain (or *.domain): {value!r}")
    if kind == "write":
        if not (value.startswith("/") or value.startswith("~/")):
            raise GrantError(f"write grant must be an absolute or ~/ path, got {value!r}")
        if os.path.normpath(os.path.expanduser(value)) in ("/", os.path.expanduser("~")):
            raise GrantError(f"write grant too broad: {value!r}")
        _not_the_labs_own(value)
    if kind == "tool" and not _TOOL.match(value):
        raise GrantError(f"not a tool name or anchored mcp__server__ glob: {value!r}")
    if kind == "tool" and value == "mcp__lab__start_run":
        raise GrantError("long runs are approved one at a time; start_run can't be granted")
    if kind == "tool" and not grantable_tool(value):
        raise GrantError(f"{value!r} can't be granted: the lab's own tools and Bash are "
                         "governed by the sandbox and the approvals")


NOT_GRANTABLE = ("Bash",)
LAB_SERVER = "mcp__lab__"


def grantable_tool(value):
    """Tools a grant may name: never the lab's own tools (some must always ask) or Bash
    (sandboxed commands need no grant; unsandboxed ones always ask)."""
    return value not in NOT_GRANTABLE and not value.startswith(LAB_SERVER)


def _in_home(value):
    """~/x for ~/x and /home/<user>/x, else None."""
    if value.startswith("~/"):
        return "~" + os.path.normpath(value[1:])
    parts = os.path.normpath(value).split("/")
    return "~/" + "/".join(parts[3:]) if len(parts) > 3 and parts[1] == "home" else None


def _not_the_labs_own(value):
    """A write grant may not reach the lab's own folders (its secrets, state, GitHub setup),
    nor contain them: a folder above a hidden one, made writable, would let it be moved."""
    from .sandbox import NEBIUS, SECRETS, STATE
    g = _in_home(value)
    if g is None:
        return
    for own in (SECRETS, STATE, NEBIUS, "~/.local/share/agentic-lab"):
        if g == own or g.startswith(own + "/") or own.startswith(g + "/"):
            raise GrantError(f"write grant overlaps the lab's own {own}: {value!r}")


def from_mapping(m, granted_by=None):
    """{domain: x} / {write: x} / {tool: x}, plus optional expires and reason."""
    if not isinstance(m, dict):
        raise GrantError(f"a grant is a mapping like {{domain: arxiv.org}}, got {m!r}")
    kinds = [k for k in m if k in KINDS]
    extra = set(m) - set(KINDS) - {"expires", "reason"}
    if len(kinds) != 1 or extra:
        raise GrantError(f"a grant has exactly one of {KINDS} (plus expires, reason): {m!r}")
    kind = kinds[0]
    validate(kind, m[kind])
    expires = m.get("expires")
    if expires is not None and not isinstance(expires, dt.datetime):
        raise GrantError(f"expires must be a datetime, got {expires!r}")
    return Grant(kind, m[kind], expires, granted_by, m.get("reason"))


def timed(kind, value, duration: dt.timedelta, granted_by, now: dt.datetime, reason=None):
    """A grant given by a human for a while ("arxiv.org for 1 hour")."""
    validate(kind, value)
    if duration <= dt.timedelta(0):
        raise GrantError("duration must be positive")
    if not granted_by:
        raise GrantError("a timed grant must name the human who gave it")
    return Grant(kind, value, now + duration, granted_by, reason)


def active(grants, now: dt.datetime):
    return [g for g in grants if g.active(now)]
