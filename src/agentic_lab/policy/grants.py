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
        why = write_refused(value)
        if why:
            raise GrantError(f"write grant {why}: {value!r}")
    if kind == "tool" and not _TOOL.match(value):
        raise GrantError(f"not a tool name or anchored mcp__server__ glob: {value!r}")
    if kind == "tool" and value == "mcp__lab__start_run":
        raise GrantError("long runs are approved one at a time; start_run can't be granted")
    if kind == "tool" and not grantable_tool(value):
        raise GrantError(f"{value!r} can't be granted: the lab's own tools and Bash are "
                         "governed by the sandbox and the approvals")


# Bash: sandboxed commands need no grant, unsandboxed ones always ask; the file tools run
# in the agent's process, outside the sandbox: writes are granted by path (`write`)
NOT_GRANTABLE = ("Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "Read")
LAB_SERVER = "mcp__lab__"


def write_refused(value):
    """Why a folder can't be granted for writing (None if it can). Judged on the path as
    the system reads it (repeated slashes, `.`, trailing slashes don't matter): never `/`
    or a whole home; in the agent's home (`~/…`) nothing whose first part starts with a dot
    (services' config and state, shell and tool settings, keys); never another home
    (`~user`, `/home/…`: the agent's own folders are written as `~/…`)."""
    if ".." in value.split("/"):
        return "with '..'"
    path = os.path.normpath(re.sub(r"/{2,}", "/", value))
    parts = [p for p in path.split("/") if p]
    if path == "/" or parts == ["~"]:
        return "too broad"
    if parts and parts[0].startswith("~"):
        if parts[0] != "~":
            return "into another user's home"
        if parts[1].startswith("."):
            return "into the home's own settings and the lab's folders"
        return None
    if parts and parts[0] == "home":
        return "into a home by its absolute path (use ~/… for the agent's own)"
    return None


def grantable_tool(value):
    """Tools a grant may name: never the lab's own tools (some must always ask) or Bash
    (sandboxed commands need no grant; unsandboxed ones always ask)."""
    return value not in NOT_GRANTABLE and not value.startswith(LAB_SERVER)


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
