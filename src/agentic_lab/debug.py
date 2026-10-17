"""`lab debug <agent>`: talk to an agent directly, in the classic Claude Code console.

Run by the admin on the agent's host (with sudo). It stops the agent's service, then opens
the agent's session (by default the one of its most recent task) in the classic Claude Code
console: as the agent's own Unix user, in its working folder, with its own sandbox settings
and seat token, and with no user/project settings loaded (exactly like the service). On
exit, the service starts again and resumes any task it was on.

Two halves, because the console must run as the agent's user:
  `lab debug <agent>`          (admin, root)    stop service -> run the session -> restart
  `lab debug-session <agent>`  (the agent user) build its settings, exec `claude --resume`
"""
import json
import os
import subprocess
import sys
from importlib import resources

from .registry import load as load_lab
from .worker import tasks as T

CONFIG = "~/.config/agentic-lab"
STATE = "~/.local/state/agentic-lab"
SERVICE = {"worker": "lab-worker@{}"}      # managers and the secretary join in M3/M4


def claude_cli():
    """The Claude Code binary the Agent SDK bundles (the same one the service runs)."""
    bundled = resources.files("claude_agent_sdk").joinpath("_bundled", "claude")
    return str(bundled) if bundled.is_file() else "claude"


def latest_session(agent_id):
    """The session of this agent's most recent task that has one, or None."""
    store = T.TaskStore(os.path.expanduser(f"{STATE}/tasks"))
    for t in reversed(store.all()):
        if t.session_id:
            return t.session_id
    return None


def debug_session(lab_path, agent_id, session=None, new=False, ask=None):
    """Runs as the agent's user: exec the classic console on the agent's session."""
    lab = load_lab(lab_path)
    if agent_id not in lab.agents:
        sys.exit(f"lab: no agent {agent_id!r}")
    a = lab.agents[agent_id]
    workdir = os.path.expanduser(a.get("workdir", "~/work"))
    settings_path = os.path.expanduser(f"{CONFIG}/debug-settings.json")
    os.makedirs(os.path.dirname(settings_path), mode=0o700, exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(lab.agent_settings(agent_id), f, indent=1)
    os.chmod(settings_path, 0o600)

    env = dict(os.environ)
    token_file = os.path.expanduser(f"{CONFIG}/claude-oauth-token")
    if os.path.exists(token_file):
        with open(token_file) as f:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = f.read().strip()

    argv = [claude_cli(), "--settings", settings_path, "--setting-sources="]
    sid = None if new else (session or latest_session(agent_id))
    if sid:
        argv += ["--resume", sid]
    if ask:
        argv += ["-p", ask]               # one question, answered non-interactively
        print(f"lab debug: opening {agent_id}'s session {sid}", flush=True)
    else:
        print(f"lab debug: no previous session; starting a new one for {agent_id}", flush=True)
    if not ask:
        print("            (sandboxed as usual; permission prompts appear here; exit to finish)\n")
    os.chdir(workdir)
    if ask:                                # nothing to read: don't wait for input
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
    os.execvpe(argv[0], argv, env)


def debug(lab_path, agent_id, session=None, new=False, ask=None):
    """Runs as root: stop the agent's service, run the console as the agent, restart."""
    if os.geteuid() != 0:
        sys.exit("lab debug needs root (it switches to the agent's user): sudo lab debug ...")
    lab = load_lab(lab_path)
    if agent_id not in lab.agents:
        sys.exit(f"lab: no agent {agent_id!r}")
    role = lab.agents[agent_id].get("role")
    unit = SERVICE.get(role, "").format(agent_id)
    if not unit:
        sys.exit(f"lab debug: {role} agents get a service in a later milestone")
    user = lab.unix_user(agent_id)

    print(f"lab debug: stopping {unit} (it resumes when you exit)", flush=True)
    subprocess.run(["systemctl", "stop", unit], check=True)
    try:
        cmd = ["sudo", "-u", user, "-H", "--", sys.executable, "-m", "agentic_lab.cli",
               "--lab", os.path.abspath(lab_path), "debug-session", agent_id]
        if session:
            cmd += ["--session", session]
        if new:
            cmd += ["--new"]
        if ask:
            cmd += ["--print", ask]
        subprocess.run(cmd)
    finally:
        print(f"\nlab debug: starting {unit} again", flush=True)
        subprocess.run(["systemctl", "start", unit], check=False)
