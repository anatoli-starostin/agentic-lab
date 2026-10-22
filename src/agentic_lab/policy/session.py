"""The agent's own Claude Code session, started with the lab's own folders covered.

Claude Code's sandbox confines the agent's Bash commands; its built-in file tools (Read,
Edit, Write, Glob, Grep) run in the `claude` process itself, where only Claude Code's
permission rules keep them off the lab's folders. So the service starts `claude` through
`lab-claude` (the SDK's cli_path): bwrap, the whole system as it is, the lab's own folders
(sandbox.lab_own) covered by empty ones, the folders above them held in place. Everything
`claude` starts inherits that from the kernel; the permission rules stay as a second layer.

Only `claude` is wrapped: the service itself reads its secrets. The seat token stays in
`claude`'s environment (it needs it); what `claude` starts can't see it, as before (the
sandbox strips it, approved commands and runs have their own process namespace). /run
isn't covered here: the resolver lives there, and approved commands, started by `claude`,
keep it (confine.py).

Nothing the agent controls reaches the launcher: its arguments come from the user database
and constants, never from the environment (bwrap isn't setuid here, so the launcher runs
before any sandbox exists).
"""
import logging
import os
import pwd
import sys
from importlib import resources

from .confine import BWRAP, covers
from .sandbox import lab_own

log = logging.getLogger(__name__)

LAUNCHER = "/opt/agentic-lab/venv/bin/lab-claude"
# No bwrap, no session (the host stops serving). False: warn and run `claude` unconfined.
REFUSE_UNCONFINED = True


class SessionError(RuntimeError):
    pass


def claude_cli():
    """The Claude Code binary the Agent SDK bundles; never one found on the PATH."""
    bundled = resources.files("claude_agent_sdk").joinpath("_bundled", "claude")
    return str(bundled) if bundled.is_file() else None


def wrapper(home, claude):
    """bwrap's argv for `claude` (the SDK's own arguments follow)."""
    home = os.path.abspath(home)
    return ([BWRAP, "--die-with-parent", "--dev-bind", "/", "/"]
            + covers(home, lab_own(home)) + ["--", claude])


def resolve(launcher=LAUNCHER, bwrap=BWRAP):
    """The cli_path for the agent's sessions, or None (Claude Code's own, unconfined)."""
    missing = [p for p in (bwrap, launcher) if not os.access(p, os.X_OK)]
    if not missing:
        return launcher
    problem = f"the agent's session can't be confined: {', '.join(missing)} missing"
    if REFUSE_UNCONFINED:
        log.error("%s; not starting it", problem)
        raise SessionError(problem + " (sudo lab apply)")
    log.warning("%s; starting it unconfined", problem)
    return None


def cli_path():
    return resolve()


def main():
    """lab-claude: exec `claude` under bwrap. As the agent's user, started by its service."""
    home = pwd.getpwuid(os.getuid()).pw_dir
    claude = claude_cli()
    if not claude or not os.access(BWRAP, os.X_OK):
        sys.exit(f"lab-claude: {'bwrap' if claude else 'the bundled claude'} is missing")
    os.execv(BWRAP, wrapper(home, claude) + sys.argv[1:])
