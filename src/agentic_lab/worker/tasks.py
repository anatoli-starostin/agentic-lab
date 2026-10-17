"""The worker's durable task queue (DESIGN.md §5).

One JSON file per task in a directory, written atomically, so tasks survive restarts of
both the worker service and the agent. Tasks run first come, first served, one at a time.

    queued -> running -> done | failed | cancelled
                 ^  |
                 |  v
          awaiting_approval          (M2: a permission request is with a human)
"""
import datetime as dt
import json
import os
import secrets
import tempfile
import threading
from dataclasses import asdict, dataclass, field

QUEUED, RUNNING, AWAITING, DONE, FAILED, CANCELLED = (
    "queued", "running", "awaiting_approval", "done", "failed", "cancelled")
STATES = (QUEUED, RUNNING, AWAITING, DONE, FAILED, CANCELLED)
TERMINAL = (DONE, FAILED, CANCELLED)
_NEXT = {
    QUEUED: {RUNNING, CANCELLED},
    RUNNING: {AWAITING, DONE, FAILED, CANCELLED, QUEUED},   # QUEUED: requeued after a crash
    AWAITING: {RUNNING, FAILED, CANCELLED},
}
MAX_PROGRESS = 200      # progress lines kept per task


class TaskError(ValueError):
    pass


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class Task:
    id: str
    instruction: str
    requester: str                  # the human who asked (through their manager)
    manager: str                    # the manager agent that submitted it
    project: str | None = None
    state: str = QUEUED
    created: str = field(default_factory=_now)
    started: str | None = None
    finished: str | None = None
    progress: list = field(default_factory=list)     # [{"at": ..., "message": ...}]
    result: dict | None = None      # {"summary": ..., "artifacts": [...]} when done
    error: str | None = None
    session_id: str | None = None   # the agent session, for resume and `lab debug`
    cancel_requested: bool = False

    def view(self, with_progress=True):
        d = asdict(self)
        if not with_progress:
            d["progress"] = d["progress"][-1:]
        return d


class TaskStore:
    """Tasks as files under `root`. Thread-safe within one process."""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, mode=0o700, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, task_id):
        if not task_id.startswith("t-") or not task_id[2:].isalnum():
            raise TaskError(f"bad task id {task_id!r}")
        return os.path.join(self.root, f"{task_id}.json")

    def _write(self, task):
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-")
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(task), f, indent=1)
        os.replace(tmp, self._path(task.id))

    def get(self, task_id) -> Task:
        try:
            with open(self._path(task_id)) as f:
                return Task(**json.load(f))
        except FileNotFoundError:
            raise TaskError(f"no task {task_id!r}") from None

    def all(self):
        with self._lock:
            tasks = []
            for name in os.listdir(self.root):
                if name.endswith(".json") and name.startswith("t-"):
                    tasks.append(self.get(name[:-5]))
            return sorted(tasks, key=lambda t: t.id)      # ids increase: FIFO order

    def submit(self, instruction, requester, manager, project=None) -> Task:
        if not instruction or not instruction.strip():
            raise TaskError("a task needs an instruction")
        with self._lock:
            task = Task(id=self._new_id(), instruction=instruction,
                        requester=requester, manager=manager, project=project)
            self._write(task)
            return task

    def _new_id(self):
        """Ids sort in submission order: a microsecond timestamp, kept strictly increasing,
        plus a random suffix so ids can't be guessed from one another."""
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")
        last = max((n[2:22] for n in os.listdir(self.root)
                    if n.startswith("t-") and n.endswith(".json")), default="")
        if stamp <= last:
            stamp = str(int(last) + 1)
        return f"t-{stamp}{secrets.token_hex(3)}"

    def next_queued(self):
        """The oldest queued task (FIFO), or None."""
        return next((t for t in self.all() if t.state == QUEUED), None)

    def update(self, task_id, **changes) -> Task:
        with self._lock:
            task = self.get(task_id)
            new_state = changes.get("state")
            if new_state is not None and new_state != task.state:
                if new_state not in _NEXT.get(task.state, set()):
                    raise TaskError(f"{task_id}: can't go from {task.state} to {new_state}")
                if new_state == RUNNING and task.started is None:
                    changes.setdefault("started", _now())
                if new_state in TERMINAL:
                    changes.setdefault("finished", _now())
            for k, v in changes.items():
                if not hasattr(task, k):
                    raise TaskError(f"unknown task field {k!r}")
                setattr(task, k, v)
            self._write(task)
            return task

    def add_progress(self, task_id, message) -> Task:
        with self._lock:
            task = self.get(task_id)
            task.progress = (task.progress + [{"at": _now(), "message": message}])[-MAX_PROGRESS:]
            self._write(task)
            return task

    def request_cancel(self, task_id) -> Task:
        """Queued tasks are cancelled at once; a running task is asked to stop."""
        with self._lock:
            task = self.get(task_id)
            if task.state == QUEUED:
                return self.update(task_id, state=CANCELLED)
            if task.state in TERMINAL:
                return task
            return self.update(task_id, cancel_requested=True)

    def recover(self):
        """After a restart: tasks left running go back to the queue (they resume their
        agent session when picked up again). Returns the requeued tasks."""
        with self._lock:
            out = []
            for t in self.all():
                if t.state in (RUNNING, AWAITING):
                    if t.state == AWAITING:
                        t = self.update(t.id, state=RUNNING)
                    out.append(self.update(t.id, state=QUEUED))
            return out

