"""Long runs: a worker's jobs that outlive its Claude session (DESIGN.md §4.2).

Anything the agent starts in a sandboxed command dies when the command returns (the sandbox
is a PID namespace) or when the task's session ends (`--die-with-parent`); anything under
the worker service dies when the service restarts (its cgroup). So a long job (training
for hours) is a *run*: the agent asks for it with the `start_run` lab tool, a human
approves it (every time: a run is outside the sandbox), and the worker service starts it
as its own systemd unit, `lab-run-<agent>@<run-id>.service`, as the agent's Unix user:

    ~/.local/state/agentic-lab/runs/<id>.json         what to run (written by the service)
    ~/.local/state/agentic-lab/runs/<id>/status.json  state and exit code (by the run)
    <workdir>/runs/<name>-<id>/log                    its output, readable by the agent

The service may start and stop only its own runs: one sudo rule per worker allows only
deploy/worker/lab-run-ctl, which checks its arguments and takes the agent from SUDO_USER
(plain rules, no argument patterns: sudo-rs has none). The unit has NoNewPrivileges, so a
run can't use that rule to start more runs.
"""
import datetime as dt
import json
import os
import re
import secrets
import signal
import subprocess
import tempfile

SYSTEMCTL = "/usr/bin/systemctl"
RUN_CTL = "/usr/local/libexec/agentic-lab/lab-run-ctl"
ACTIVE = ("starting", "running")
_ID = re.compile(r"^r-[0-9a-f]{12}$")
_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
START_GRACE_S = 120         # a run whose unit never started is "failed" after this


class RunError(ValueError):
    pass


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def unit(agent_id, run_id):
    return f"lab-run-{agent_id}@{run_id}.service"


STATUS_KEYS = ("started", "ended", "exit_code", "pid", "stop_requested", "error")


def unit_template(agent_id, user, lab_path="/etc/agentic-lab/lab.yaml", home=None,
                  workdir=None):
    """/etc/systemd/system/lab-run-<agent>@.service: one run of this worker. The run can't
    see the agent's lab secrets (its member's Claude seat token, lab tokens), and of the
    home it may write only the working folder and its own status folder: the rest of the
    home is read-only to it. The real home path: in a system unit %h is root's."""
    home = home or f"/home/{user}"
    workdir = workdir or f"{home}/work"
    return f"""# A long run of worker {agent_id} (worker/runs.py). Started and stopped by the worker
# service only (lab-run-ctl, /etc/sudoers.d/agentic-lab-runs); never enabled.
[Unit]
Description=Agentic Lab run %i of {agent_id}

[Service]
Type=simple
User={user}
Group={user}
WorkingDirectory=~
ExecStart=/opt/agentic-lab/venv/bin/lab --lab {lab_path} worker run-exec {agent_id} %i
NoNewPrivileges=yes
PrivateTmp=yes
UMask=0077
InaccessiblePaths=-{home}/.config/agentic-lab
ReadOnlyPaths={home}
ReadWritePaths=-{workdir} -{home}/.local/state/agentic-lab/runs/%i
KillMode=control-group
TimeoutStopSec=60
"""


def sudo_rule(user):
    """The worker's one sudo rule: lab-run-ctl (its own runs, nothing else)."""
    return f"{user} ALL=(root) NOPASSWD: {RUN_CTL}\n"


class RunStore:
    def __init__(self, root, workdir, agent_id, systemctl=None):
        self.root, self.workdir, self.agent_id = root, workdir, agent_id
        os.makedirs(root, mode=0o700, exist_ok=True)
        # how to start/stop/ask about units (tests replace it)
        self.systemctl = systemctl or _systemctl

    def _path(self, run_id, suffix=""):
        if not _ID.match(run_id or ""):
            raise RunError(f"bad run id {run_id!r}")
        if suffix == ".status":     # a folder of its own: the one place its run may write
            return os.path.join(self.root, run_id, "status.json")
        return os.path.join(self.root, f"{run_id}{suffix}.json")

    def _status(self, run_id):
        """A run's status: only its status fields (it can't change what the run is)."""
        st = self._read(self._path(run_id, ".status"))
        if st is None:              # a run started before its status had a folder
            st = self._read(os.path.join(self.root, f"{run_id}.status.json"))
        st = st if isinstance(st, dict) else {}
        return {k: v for k, v in st.items() if k in STATUS_KEYS + ("state",)}

    def _write(self, path, data):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)

    def _read(self, path):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def start(self, task_id, name, command, cwd=None):
        """Start a run (already approved). Returns its view."""
        if not (command or "").strip():
            raise RunError("a run needs a command")
        name = _NAME.sub("-", (name or "run").strip())[:40].strip("-") or "run"
        cwd = os.path.normpath(os.path.join(self.workdir, os.path.expanduser(cwd or ".")))
        if not os.path.isdir(cwd):
            raise RunError(f"no folder {cwd}")
        run_id = f"r-{secrets.token_hex(6)}"
        logdir = os.path.join(self.workdir, "runs", f"{name}-{run_id}")
        os.makedirs(logdir, exist_ok=True)
        spec = {"id": run_id, "task": task_id, "name": name, "command": command, "cwd": cwd,
                "log": os.path.join(logdir, "log"), "created": _now()}
        self._write(self._path(run_id), spec)
        os.makedirs(os.path.dirname(self._path(run_id, ".status")), mode=0o700, exist_ok=True)
        try:
            self.systemctl("start", self.agent_id, run_id)
        except (OSError, subprocess.SubprocessError) as e:
            self._write(self._path(run_id, ".status"),
                        {"state": "failed", "ended": _now(), "error": f"couldn't start: {e}"})
        return self.view(run_id)

    def stop(self, run_id):
        self.get(run_id)
        self.systemctl("stop", self.agent_id, run_id)
        st = self._status(run_id)
        if st.get("state") not in ("exited", "failed", "stopped", "lost"):
            self._write(self._path(run_id, ".status"), {**st, "stop_requested": _now()})
        return self.view(run_id)

    def get(self, run_id):
        spec = self._read(self._path(run_id))
        if spec is None:
            raise RunError(f"no run {run_id!r}")
        return spec

    def view(self, run_id, tail=0):
        """The run with its state: starting, running, exited (exit code), failed, stopped,
        or lost (its unit ended without a word, e.g. the machine was powered off)."""
        spec = self.get(run_id)
        st = self._status(run_id)
        state = st.get("state") or "starting"
        if state in ACTIVE and not self.systemctl("is-active", self.agent_id, run_id):
            age = (dt.datetime.now(dt.timezone.utc)
                   - dt.datetime.fromisoformat(spec["created"])).total_seconds()
            if state == "running" or age > START_GRACE_S:
                state = "lost"
        out = {**spec, **{k: v for k, v in st.items() if k != "state"}, "state": state}
        if tail:
            out["log_tail"] = log_tail(spec["log"], tail)
        return out

    def all(self, task_id=None):
        ids = sorted(n[:-5] for n in os.listdir(self.root)
                     if _ID.match(n[:-5]) and n.endswith(".json"))
        runs = [self.view(i) for i in ids]
        return [r for r in runs if task_id is None or r["task"] == task_id]

    def active(self, task_id=None):
        return [r for r in self.all(task_id) if r["state"] in ACTIVE]


def log_tail(path, lines=40):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 64 * 1024))
            return "\n".join(f.read().decode(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def home_env(workdir):
    """A run's home is read-only to it: what tools keep in the home (settings, data, state)
    goes into the working folder instead."""
    w = os.path.abspath(workdir)
    return {"XDG_CONFIG_HOME": os.path.join(w, ".config"),
            "XDG_DATA_HOME": os.path.join(w, ".local", "share"),
            "XDG_STATE_HOME": os.path.join(w, ".local", "state")}


def _systemctl(verb, agent_id, run_id):
    if verb == "is-active":
        r = subprocess.run([SYSTEMCTL, "is-active", "--quiet", unit(agent_id, run_id)],
                           timeout=10)
        return r.returncode == 0
    subprocess.run(["sudo", "-n", RUN_CTL, verb, run_id], check=True, capture_output=True,
                   timeout=30)
    return True


def run_exec(store: RunStore, run_id, env=None):
    """The unit's main process (as the agent's user, outside the sandbox): run the command,
    output to its log, and record how it ended."""
    spec = store.get(run_id)
    status = store._path(run_id, ".status")
    with open(spec["log"], "ab") as log:
        log.write(f"== {_now()} run {run_id}: {spec['command']}\n".encode())
        log.flush()
        from ..policy.sandbox import cache_env      # the same caches as its tasks (#45)
        p = subprocess.Popen(["/bin/bash", "-c", spec["command"]], cwd=spec["cwd"],
                             stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True,
                             env={**os.environ, **cache_env(store.workdir),
                                  **home_env(store.workdir), **(env or {})})
        store._write(status, {"state": "running", "started": _now(), "pid": p.pid})
        stopped = {}

        def on_term(signum, frame):
            stopped["at"] = _now()
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        signal.signal(signal.SIGTERM, on_term)
        code = p.wait()
        st = store._status(run_id)
        state = "stopped" if stopped or st.get("stop_requested") else "exited"
        store._write(status, {**st, "state": state, "ended": _now(), "exit_code": code})
        log.write(f"== {_now()} {state}, exit code {code}\n".encode())
    return 0
