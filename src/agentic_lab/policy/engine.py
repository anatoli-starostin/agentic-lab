"""The classifier: one tool call + a Policy -> a Verdict.

    ungated : read-only tool -> defer to Claude Code (auto-approves it).
    green   : inside the agent's safe zone -> auto-allow, no human.
    gated   : everything else -> a human must approve (the broker routes it).

The rule is "auto-allow only the trivially simple / known safe; gate on anything else".
The failure mode is always toward asking, never toward silent broad execution. Every
verdict carries the reason, so `lab policy explain` can show which rule decided.
"""
import os
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase

from .model import Policy, _in
from .shell import escape_ops, segments

UNGATED, GREEN, GATED = "ungated", "green", "gated"


@dataclass(frozen=True)
class Verdict:
    decision: str
    reason: str

    def __str__(self):
        return f"{self.decision}: {self.reason}"


# ── tiers ─────────────────────────────────────────────────────────────────────

def _sbox_ok(p: Policy, seg):
    """A single `sbox <argv>`, or `sbox --net <mode> -- <argv>` for an allowed mode.
    `--net` counts only as sbox's OWN first argument (that is all the wrapper parses);
    later it is just an argument of the caged program and carries no network."""
    if not p.sbox_enabled or len(seg) < 2 or seg[0] != "sbox":
        return None
    if seg[1] == "--net":
        if len(seg) >= 5 and seg[2] in p.sbox_net_modes and seg[3] == "--":
            return f"sbox with allowed network mode {seg[2]!r}"
        return None
    return "single clean sbox command (no network)"


# a token naming a network endpoint: scheme://..., ext::..., file://..., or scp user@host:path
_URL_RE = re.compile(r"://|^ext::|^file://|^[\w.+-]+@[\w.-]+:")
_GIT_OPT_WITH_ARG = {"-C", "--git-dir", "--work-tree", "--namespace"}
_GIT_EXEC_OPTS = ("--upload-pack", "--receive-pack", "--exec")


def _git_ok(p: Policy, seg):
    """git limited to the configured subcommands, with the code-exec / exfil sharp edges
    excluded whatever the config says: no `-c`, no --exec / --upload-pack /
    --receive-pack, and no explicit URL / ext:: / scp remote."""
    if not p.git_enabled or not seg or seg[0] != "git" or len(seg) < 2:
        return None
    i, sub = 1, None
    while i < len(seg):
        t = seg[i]
        if t.startswith("-c") or t.startswith("--exec") \
           or t.startswith("--upload-pack") or t.startswith("--receive-pack"):
            return None
        if t in _GIT_OPT_WITH_ARG:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        sub = t
        break
    if sub not in p.git_subcommands:
        return None
    # options that run a program, wherever they appear (spiky's rule only looked before
    # the subcommand, so `git fetch --upload-pack=evil` and `git rebase -x evil` ran)
    rest = seg[i + 1:]
    if any(t.startswith(_GIT_EXEC_OPTS) for t in rest):
        return None
    if sub == "rebase" and any(t == "-x" or (t.startswith("-") and not t.startswith("--")
                                             and "x" in t[1:]) for t in rest):
        return None
    if any(_URL_RE.search(t) for t in seg[1:]):
        return None
    # `--output=<file>` (diff, log, format-patch) writes to an arbitrary path
    if any(t.startswith("--output") for t in seg[1:]):
        return None
    return f"scoped-safe git {sub!r}"


_PY = {"python", "python3"}


def _interpreter_ok(p: Policy, prog):
    """`python3` from PATH, or an absolute python OUTSIDE the writable zone. A python the
    agent can write (`~/projects/evil/python3`) would run its own code uncaged."""
    if prog in _PY:
        return True
    return (os.path.basename(prog) in _PY and os.path.isabs(prog)
            and not _in(p.writable, os.path.realpath(prog)))


def _helper_ok(p: Policy, seg):
    for h in p.helpers:
        if seg and seg[0] == h.path:
            args = seg[1:]
        elif len(seg) >= 2 and seg[1] == h.path and _interpreter_ok(p, seg[0]):
            args = seg[2:]
        else:
            continue
        if h.subcommands is not None and (not args or args[0] not in h.subcommands):
            continue
        return f"trusted helper {h.path}"
    return None


# An ID argument for a helper's heredoc form: a plain word, never shell syntax.
_ID = r"[A-Za-z0-9_.:-]+"


def _helper_heredoc_ok(p: Policy, command):
    """`[python3] <helper> <sub> <id> [<<'EOF' ... EOF]` matched as a WHOLE line, so the
    report text (literal data inside a QUOTED heredoc) never reaches the shell parser.
    The id is a plain word, so `done x;curl evil` cannot ride along."""
    lines = command.split("\n")
    for h in p.helpers:
        if not h.allow_quoted_heredoc:
            continue
        subs = "|".join(map(re.escape, h.subcommands)) if h.subcommands else r"\S+"
        head = re.compile(
            # optional interpreter, whose directory may hold only path characters (a
            # loose `\S+/` would let `evil;/python3 ...` smuggle `evil` in front)
            r"^\s*(?:((?:/[\w.+-]+)*/?python3?)\s+)?" + re.escape(h.path)
            + r"\s+(?:" + subs + r")\s+" + _ID + r"\s*"
            + r"(<<-?\s*(['\"])([A-Za-z_][A-Za-z0-9_]*)\3\s*)?$")
        m = head.match(lines[0])
        if not m or (m.group(1) and not _interpreter_ok(p, m.group(1))):
            continue
        delim = m.group(4)
        if not delim:
            if len(lines) == 1 or not "".join(lines[1:]).strip():
                return f"trusted helper {h.path}"
            continue
        body = lines[1:]
        while body and not body[-1].strip():
            body.pop()
        # the delimiter must close the heredoc exactly once, on the last line: an earlier
        # delimiter line would end the data there and run the lines after it as commands
        if body and body[-1].strip() == delim and all(l.strip() != delim for l in body[:-1]):
            return f"trusted helper {h.path} (quoted heredoc)"
    return None


def _seg_reason(p: Policy, seg):
    line = " ".join(seg)
    for pat in p.safe_patterns:
        if re.match(pat, line):
            return f"read-only command (pattern {pat!r})"
    return _git_ok(p, seg) or _helper_ok(p, seg)


def classify_bash(p: Policy, command) -> Verdict:
    r = _helper_heredoc_ok(p, command)
    if r:
        return Verdict(GREEN, r)
    segs = segments(command)
    if segs is None:
        return Verdict(GATED, "unparseable shell line")
    if not segs:
        return Verdict(GREEN, "empty command")
    # No redirect / substitution / background may ride along on an auto-allowed line
    # (those escape the cage or spawn uncaged work). Pipes and chains are fine, because
    # every segment is checked on its own.
    if escape_ops(command):
        return Verdict(GATED, "outer redirect, command substitution or background '&'")
    if len(segs) == 1:
        r = _sbox_ok(p, segs[0])
        if r:
            return Verdict(GREEN, r)
    reasons = []
    for seg in segs:
        r = _seg_reason(p, seg)
        if not r:
            what = "segment" if len(segs) > 1 else "command"
            if seg[0] == "git" and p.git_enabled:
                return Verdict(GATED, f"{what} 'git' is outside this agent's git rules "
                                      "(subcommand not allowed, or -c / URL / exec option)")
            return Verdict(GATED, f"{what} {seg[0]!r} is not in the green zone")
        reasons.append(r)
    return Verdict(GREEN, "; ".join(dict.fromkeys(reasons)))


def _matches(name, patterns):
    return any(fnmatchcase(name, pat) for pat in patterns)


def _in_writable(p: Policy, path):
    if not path:
        return False
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        return False
    return _in(p.writable, os.path.realpath(expanded))


def classify(p: Policy, tool, tool_input=None) -> Verdict:
    ti = tool_input or {}
    if tool in p.file_tools:
        path = ti.get("file_path") or ti.get("notebook_path")
        if _in_writable(p, path):
            return Verdict(GREEN, f"write inside the writable zone ({path})")
        return Verdict(GATED, f"write outside the writable zone ({path})")
    if tool == "Bash":
        return classify_bash(p, ti.get("command", "") or "")
    if tool == "Monitor":
        cmd = (ti.get("command") or "").strip()
        if not ti.get("ws") and cmd in p.monitor_commands:
            return Verdict(GREEN, "trusted Monitor command")
        return Verdict(GATED, "Monitor command not in monitor_commands")
    if _matches(tool, p.ungated_tools):
        return Verdict(UNGATED, f"read-only tool {tool!r}")
    if _matches(tool, p.green_tools):
        return Verdict(GREEN, f"tool {tool!r} is green for this agent")
    return Verdict(GATED, f"tool {tool!r} is not known safe")
