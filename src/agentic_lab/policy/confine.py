"""Approved one-off commands outside the sandbox (Bash with `dangerouslyDisableSandbox`).

A human approved the command for what it does on the machine: it keeps everything the
agent's user may do there (files, network, devices), exactly as approved. Two things it
doesn't get, because they're the lab's and not part of any approval: the lab's own
credentials and state (the agent's lab secrets, its GitHub tokens, the service's state),
and a view of the service's processes (their environments hold the seat token). So it
runs in a process namespace of its own (bwrap, as the sandbox does), with those folders
covered by empty ones and the seat token's variable unset. The folders above each covered
one (up to the home) are bound onto themselves: still fully writable, but a mount point
can't be renamed or removed, so nothing can move a covered folder away. Nothing about the
approval changes: the human sees and decides on the command as the agent asked for it;
only how it's started differs.
"""
import glob
import os
import shlex

from .sandbox import NEBIUS, SECRET_ENV, SECRETS, STATE

BWRAP = "/usr/bin/bwrap"
GITHUB_IDENTITIES = "~/.local/share/agentic-lab/github/*"   # each identity's folder


def _plain_dir(p):
    return os.path.isdir(p) and not os.path.islink(p)


def confined(command, home=None):
    """`command` as it's run: in its own process namespace, the lab's own credentials and
    state hidden, nothing else changed."""
    home = os.path.abspath(home or os.path.expanduser("~"))
    at = lambda p: p.replace("~", home, 1)                    # noqa: E731
    covered = [d for d in [at(SECRETS), at(STATE), at(NEBIUS)]
               + sorted(glob.glob(at(GITHUB_IDENTITIES))) if _plain_dir(d)]
    held = []                   # the folders above them, up to the home: kept in place
    for d in covered:
        parent = os.path.dirname(d)
        while parent.startswith(home + os.sep):
            if parent not in held and parent not in covered:
                held.append(parent)
            parent = os.path.dirname(parent)
    args = [BWRAP, "--die-with-parent", "--unshare-pid", "--dev-bind", "/", "/",
            "--proc", "/proc"]
    for d in sorted(held, key=lambda p: p.count(os.sep)):
        args += ["--bind", d, d]
    for d in covered:
        args += ["--tmpfs", d]
    for name in SECRET_ENV:
        args += ["--unsetenv", name]
    return shlex.join(args + ["--", "/bin/bash", "-c", command])


def approved_input(tool_name, tool_input, home=None):
    """The input to run an approved call with (None: as asked). Only a Bash command that
    leaves the sandbox changes."""
    if tool_name == "Bash" and (tool_input or {}).get("dangerouslyDisableSandbox"):
        return {**tool_input, "command": confined(tool_input.get("command", ""), home)}
    return None
