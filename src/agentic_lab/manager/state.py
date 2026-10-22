"""What a manager remembers between messages and restarts.

  conversations: a Slack conversation (a DM, or a thread) -> its Claude session
  tasks:         a task it submitted -> the worker, the conversation to report to, and what
                 was last reported (progress lines, state)
"""
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field


@dataclass
class Conversation:
    key: str                    # "<channel>" for a DM, "<channel>/<thread_ts>" for a thread
    channel: str
    thread_ts: str | None       # where replies go (None: top level of a DM)
    session_id: str | None = None
    last_requester: str | None = None   # the human of the latest message
    # guest turns since the owner's last turn here, for its next one ("meanwhile...")
    meanwhile: list = field(default_factory=list)


@dataclass
class TrackedTask:
    task_id: str
    worker: str
    conversation: str           # Conversation.key
    requester: str
    progress_seen: int = 0
    state: str = "queued"
    done: bool = False
    approval_noted: bool = False
    registered: bool = True     # with the broker (approvals reach the requester); retried
    misses: int = 0             # rounds the worker didn't know the task
    wrapup_reply: str | None = None     # the wrap-up written, not yet delivered
    wrapup_tries: int = 0               # failed wrap-up attempts
    wrapup_next: float = 0.0            # not before this (time.time()), after a failure


class ManagerState:
    def __init__(self, root):
        self.path = os.path.join(root, "manager-state.json")
        os.makedirs(root, mode=0o700, exist_ok=True)
        self._lock = threading.RLock()
        self.conversations: dict[str, Conversation] = {}
        self.tasks: dict[str, TrackedTask] = {}
        self.engaged = PersistentSet(self.save)     # channel threads it takes part in
        self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
        except (FileNotFoundError, ValueError):
            return
        self.conversations = {k: Conversation(**v) for k, v in d.get("conversations", {}).items()}
        self.tasks = {k: TrackedTask(**v) for k, v in d.get("tasks", {}).items()}
        self.engaged.update_quietly(d.get("engaged", []))

    def save(self):
        with self._lock:
            d = {"conversations": {k: asdict(v) for k, v in self.conversations.items()},
                 "tasks": {k: asdict(v) for k, v in self.tasks.items()},
                 "engaged": sorted(self.engaged)}
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".tmp-")
            with os.fdopen(fd, "w") as f:
                json.dump(d, f, indent=1)
            os.replace(tmp, self.path)

    def conversation(self, channel, thread_ts):
        key = channel if thread_ts is None else f"{channel}/{thread_ts}"
        with self._lock:
            if key not in self.conversations:
                self.conversations[key] = Conversation(key, channel, thread_ts)
                self.save()
            return self.conversations[key]

    def track(self, task_id, worker, conversation, requester, registered=True):
        with self._lock:
            self.tasks[task_id] = TrackedTask(task_id, worker, conversation, requester,
                                              registered=registered)
            self.save()

    def open_tasks(self):
        return [t for t in self.tasks.values() if not t.done]


class PersistentSet(set):
    """A set that saves the state when something is added."""

    def __init__(self, save):
        super().__init__()
        self._save = save

    def add(self, item):
        if item not in self:
            super().add(item)
            self._save()

    def update_quietly(self, items):
        super().update(items)
