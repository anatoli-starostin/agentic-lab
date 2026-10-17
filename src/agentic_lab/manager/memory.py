"""A manager's memory across conversations, kept by code.

A dedicated manager has one memory. A shared manager keeps a separate memory per member,
so what one member says never reaches another. Channel threads use only their own context
(no memory). The memory lives in the service's state folder, which the agent can't read
directly; it reaches it only through the `recall` / `remember` tools, and the code picks
the file for the member of the current conversation.
"""
import datetime as dt
import os
import re

MAX_BYTES = 64 * 1024       # memory is notes, not an archive


class Memory:
    def __init__(self, root, shared):
        self.root = root
        self.shared = shared
        os.makedirs(root, mode=0o700, exist_ok=True)

    def path(self, member, private_conversation):
        """The memory file for this conversation, or None (channel threads have none)."""
        if not private_conversation:
            return None
        if not self.shared:
            return os.path.join(self.root, "notes.md")
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", member or ""):
            return None
        return os.path.join(self.root, f"member-{member}.md")

    def recall(self, member, private_conversation):
        p = self.path(member, private_conversation)
        if p is None:
            return "(no memory in channel threads: only this thread's own context counts)"
        try:
            with open(p) as f:
                return f.read() or "(nothing remembered yet)"
        except FileNotFoundError:
            return "(nothing remembered yet)"

    def remember(self, member, private_conversation, note):
        p = self.path(member, private_conversation)
        if p is None:
            return "not remembered: channel threads keep no memory"
        stamp = dt.date.today().isoformat()
        entry = f"- {stamp}: {note.strip()}\n"
        size = os.path.getsize(p) if os.path.exists(p) else 0
        if size + len(entry) > MAX_BYTES:
            return "memory is full; ask the member what can be forgotten"
        with open(p, "a") as f:
            f.write(entry)
        os.chmod(p, 0o600)
        return "remembered"
