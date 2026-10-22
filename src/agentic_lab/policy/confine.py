"""Approved one-off commands outside the sandbox (Bash with `dangerouslyDisableSandbox`).

A human approved the command for what it does on the machine: it keeps everything the
agent's user may do there (files, network, devices), exactly as approved. Two things it
doesn't get, because they're the lab's and not part of any approval: the lab's own
credentials and state (the agent's lab secrets, its GitHub tokens, the service's state),
and a view of the service's processes (their environments hold the seat token). So it
runs in a process namespace of its own (bwrap, as the sandbox does), with those folders
and files covered by empty ones and the seat token's variable unset. Nothing about the
approval changes: the human sees and decides on the command as the agent asked for it;
only how it's started differs.
"""
import glob
import os
import shlex

from .sandbox import NEBIUS, SECRET_ENV, SECRETS, STATE

BWRAP = "/usr/bin/bwrap"
GITHUB_TOKENS = "~/.local/share/agentic-lab/github/*/token"


def _plain_dir(p):
    return os.path.isdir(p) and not os.path.islink(p)


def confined(command, home=None):
    """`command` as it's run: in its own process namespace, the lab's own credentials and
    state hidden, nothing else changed."""
    home = home or os.path.expanduser("~")
    at = lambda p: p.replace("~", home, 1)                    # noqa: E731
    args = [BWRAP, "--die-with-parent", "--unshare-pid", "--dev-bind", "/", "/",
            "--proc", "/proc"]
    for d in (at(SECRETS), at(STATE), at(NEBIUS)):
        if _plain_dir(d):
            args += ["--tmpfs", d]
    for f in sorted(glob.glob(at(GITHUB_TOKENS))):
        if os.path.isfile(f) and not os.path.islink(f):
            args += ["--ro-bind", "/dev/null", f]
    for name in SECRET_ENV:
        args += ["--unsetenv", name]
    return shlex.join(args + ["--", "/bin/bash", "-c", command])


def approved_input(tool_name, tool_input, home=None):
    """The input to run an approved call with (None: as asked). Only a Bash command that
    leaves the sandbox changes."""
    if tool_name == "Bash" and (tool_input or {}).get("dangerouslyDisableSandbox"):
        return {**tool_input, "command": confined(tool_input.get("command", ""), home)}
    return None
