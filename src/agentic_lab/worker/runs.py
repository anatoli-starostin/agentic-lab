"""Long runs: a worker's jobs that outlive its Claude session (DESIGN.md §4.2).

Anything the agent starts in a sandboxed command dies when the command returns (the sandbox
is a PID namespace) or when the task's session ends (`--die-with-parent`); anything under
the worker service dies when the service restarts (its cgroup). So a long job (training
for hours) is a *run*: the agent asks for it with the `start_run` lab tool, a human
approves it (every time: a run is outside the sandbox), and the worker service starts it
as its own systemd unit, `lab-run-<agent>@<run-id>.service`, as the agent's Unix user:

    ~/.local/state/agentic-lab/runs/<id>.json         what to run (written by the service)
    ~/.local/state/agentic-lab/runs/<id>/status.json  state and exit code (by run-exec)
    ~/.local/state/agentic-lab/run-home/<member>/     the job's home (one per member)
    <workdir>/runs/<name>-<id>/log                    its output, readable by the agent

The unit's main process is `lab worker run-exec` (ours): it reads the spec, writes the
status, and starts only the job under bwrap, in a process namespace of its own, with a home
of its own: the member's run home mounted over the real home, the working folder bound back
on top, the agent's GitHub setup read-only. The lab's secrets and state aren't in that home
at all, and every tool that writes to ~ just works there (docs/long-runs.md).

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

from .. import safefs

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


BWRAP = "/usr/bin/bwrap"
GITHUB = "~/.local/share/agentic-lab/github"    # the agent's git setup: read-only in a run
_MEMBER = re.compile(r"^[a-z][a-z0-9-]*$")
STATUS_KEYS = ("started", "ended", "exit_code", "pid", "stop_requested", "error")


def unit_template(agent_id, user, lab_path="/etc/agentic-lab/lab.yaml", home=None,
                  workdir=None):
    """/etc/systemd/system/lab-run-<agent>@.service: one run of this worker. Two layers:
    run-exec gives the job a home of its own under bwrap (the main protection, see
    run_exec); underneath, the unit itself keeps the real home read-only (only the working
    folder, the run's status and the run homes writable) and the lab's secrets hidden, so a
    mistake in the job's mounts falls back to that, never below. The real home path: in a
    system unit %h is root's."""
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
# run-exec starts the job itself, under bwrap (a home and a process view of its own)
ExecStart=/opt/agentic-lab/venv/bin/lab --lab {lab_path} worker run-exec {agent_id} %i
NoNewPrivileges=yes
PrivateTmp=yes
UMask=0077
InaccessiblePaths=-{home}/.config/agentic-lab
ReadOnlyPaths={home}
# all of run-home/ is writable here; what keeps one member's runs out of another's run
# home is bwrap (each job sees only its member's), this directive is the layer underneath
ReadWritePaths=-{workdir} -{home}/.local/state/agentic-lab/runs/%i -{home}/.local/state/agentic-lab/run-home
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

    def run_home(self, member):
        """The home a member's runs see (one per member: what a run keeps there, e.g. a
        login, stays theirs on a shared worker). Next to the runs, never in the workdir
        (the agent's sandbox writes that, and readers read it)."""
        if not _MEMBER.match(member or ""):
            raise RunError(f"bad member {member!r}")
        return os.path.join(os.path.dirname(os.path.abspath(self.root)), "run-home", member)

    def start(self, task_id, name, command, cwd=None, requester=None):
        """Start a run (already approved) for task `task_id` of member `requester` (from the
        task, never from the job). Returns its view. Without a member its job won't start
        (run_exec fails closed)."""
        if not (command or "").strip():
            raise RunError("a run needs a command")
        home = self.run_home(requester) if requester is not None else None
        name = _NAME.sub("-", (name or "run").strip())[:40].strip("-") or "run"
        cwd = os.path.normpath(os.path.join(self.workdir, os.path.expanduser(cwd or ".")))
        if not os.path.isdir(cwd):
            raise RunError(f"no folder {cwd}")
        run_id = f"r-{secrets.token_hex(6)}"
        logdir = safefs.makedirs(self.workdir, os.path.join("runs", f"{name}-{run_id}"))
        spec = {"id": run_id, "task": task_id, "requester": requester, "name": name,
                "command": command, "cwd": cwd, "log": os.path.join(logdir, "log"),
                "created": _now()}
        self._write(self._path(run_id), spec)
        if home:
            os.makedirs(home, mode=0o700, exist_ok=True)
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
        active = False
        if (st.get("state") or "starting") in ACTIVE:
            # systemd, then the status file again: a run that ends between the two reads
            # then shows its exit, never "lost" (#59); ended runs aren't asked about
            active = self.systemctl("is-active", self.agent_id, run_id)
            st = self._status(run_id) or st
        state = st.get("state") or "starting"
        if state in ACTIVE and not active:
            st = self._status(run_id) or st     # once more: just ended?
            state = st.get("state") or "starting"
        if state in ACTIVE and not active and st.get("stop_requested"):
            state = "stopped"               # stopped before it could say so itself
        if state in ACTIVE and not active:
            age = (dt.datetime.now(dt.timezone.utc)
                   - dt.datetime.fromisoformat(spec["created"])).total_seconds()
            if state == "running" or age > START_GRACE_S:
                state = "lost"
        out = {**spec, **{k: v for k, v in st.items() if k != "state"}, "state": state}
        if tail:
            out["log_tail"] = log_tail(spec["log"], tail, root=self.workdir)
        return out

    def all(self, task_id=None):
        ids = sorted(n[:-5] for n in os.listdir(self.root)
                     if _ID.match(n[:-5]) and n.endswith(".json"))
        runs = [self.view(i) for i in ids]
        return [r for r in runs if task_id is None or r["task"] == task_id]

    def active(self, task_id=None):
        return [r for r in self.all(task_id) if r["state"] in ACTIVE]


def log_tail(path, lines, root):
    """The end of a run's log. It's in the agent's working folder (`root`), which the
    agent can change at will: read only a plain file inside it (safefs)."""
    try:
        data = safefs.tail(root, path, 64 * 1024)
        return "\n".join(data.decode(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def job_wrapper(home, run_home, workdir, cwd, github=None):
    """bwrap's arguments for the job (before its command). Order matters: the member's run
    home goes over the real home first, then the real working folder is bound back on top
    (bwrap takes sources from the host, so this is the real one even inside the home), then
    the git setup read-only. The lab's secrets and state aren't in the run home: protected
    by absence. No --die-with-parent: a stop's TERM reaches the job (KillMode=control-group
    signals every process of the unit), which then has its grace period."""
    home, workdir = os.path.abspath(home), os.path.abspath(workdir)
    github = github or GITHUB.replace("~", home, 1)
    return [BWRAP, "--unshare-pid", "--dev-bind", "/", "/", "--proc", "/proc",
            "--bind", run_home, home,
            "--bind", os.path.realpath(workdir), workdir,
            "--ro-bind-try", github, github,
            "--chdir", cwd, "--"]


STOP_POLL_S = 0.5
STOP_WAIT_MAX_S = 600       # systemd's TimeoutStopSec ends it long before; just a bound


def own_cgroup_procs():
    """The cgroup.procs file of this process's cgroup (cgroup v2), or None."""
    try:
        with open("/proc/self/cgroup") as f:
            for line in f:
                if line.startswith("0::"):
                    return os.path.join("/sys/fs/cgroup", line[3:].strip().lstrip("/"),
                                        "cgroup.procs")
    except OSError:
        pass
    return None


def wait_alone(procs_file, me=None, poll=STOP_POLL_S, limit=STOP_WAIT_MAX_S, sleep=None):
    """Wait until this process is the only one left in its cgroup (the job and everything
    it started have ended). Returns True if so, False if it can't tell (no or unreadable
    file: the caller records at once) or the bound passed."""
    import time
    me, sleep = me or os.getpid(), sleep or time.sleep
    waited = 0.0
    while waited <= limit:
        try:
            with open(procs_file) as f:
                pids = {int(x) for x in f.read().split()}
        except (OSError, ValueError, TypeError):
            return False
        if pids <= {me}:
            return True
        sleep(poll)
        waited += poll
    return False


def _systemctl(verb, agent_id, run_id):
    if verb == "is-active":
        r = subprocess.run([SYSTEMCTL, "is-active", "--quiet", unit(agent_id, run_id)],
                           timeout=10)
        return r.returncode == 0
    subprocess.run(["sudo", "-n", RUN_CTL, verb, run_id], check=True, capture_output=True,
                   timeout=30)
    return True


def run_exec(store: RunStore, run_id, env=None, wrapper=job_wrapper, home=None,
             procs_file=own_cgroup_procs):
    """The unit's main process (as the agent's user, outside the sandbox): start the job
    under bwrap (`wrapper`, job_wrapper), output to its log, and record how it ended. It
    never runs a job unwrapped: no bwrap, no member, no run home, no job."""
    spec = store.get(run_id)
    status = store._path(run_id, ".status")
    home = home or os.path.expanduser("~")
    with os.fdopen(safefs.open_append(store.workdir, spec["log"]), "ab") as log:
        log.write(f"== {_now()} run {run_id}: {spec['command']}\n".encode())
        log.flush()
        try:
            run_home = store.run_home(spec.get("requester"))
            if not os.path.isdir(run_home):
                raise RunError(f"no run home {run_home}")
            if wrapper is job_wrapper and not os.access(BWRAP, os.X_OK):
                raise RunError(f"{BWRAP} is missing: a run never starts without it")
            wrap = wrapper(home, run_home, store.workdir, spec["cwd"])
        except RunError as e:
            store._write(status, {"state": "failed", "ended": _now(), "error": str(e)})
            log.write(f"== {_now()} not started: {e}\n".encode())
            return 1
        from ..policy.sandbox import cache_env      # the same caches as its tasks (#45)
        p = subprocess.Popen(wrap + ["/bin/bash", "-c", spec["command"]], cwd=spec["cwd"],
                             stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True,
                             env={**os.environ, **cache_env(store.workdir), **(env or {})})
        store._write(status, {"state": "running", "started": _now(), "pid": p.pid})
        stopped = {}

        def on_term(signum, frame):
            # Only noted: a stop (KillMode=control-group) already sends TERM to every process
            # in the unit, the job's included. Passing it on as well made the job see it
            # twice. We wait for the job to finish (its grace period) and record that.
            stopped["at"] = _now()
        signal.signal(signal.SIGTERM, on_term)
        code = p.wait()
        st = store._status(run_id)
        if stopped:
            # A stop: bwrap ends at the first TERM, but the job is still saving. If we ended
            # now, systemd would see the main process gone and TERM the rest of the unit
            # again (the job would get it twice). So we stay until the job is gone too:
            # the run is "stopped" meanwhile, "ended" is when the job really ended.
            store._write(status, {**st, "state": "stopped"})
            wait_alone(procs_file() if callable(procs_file) else procs_file)
            # the job's own exit code isn't ours to see once bwrap is gone (its init has it)
            code = None if code < 0 else code
        state = "stopped" if stopped or st.get("stop_requested") else "exited"
        store._write(status, {**st, "state": state, "ended": _now(), "exit_code": code})
        log.write(f"== {_now()} {state}, exit code {code}\n".encode())
    return 0
