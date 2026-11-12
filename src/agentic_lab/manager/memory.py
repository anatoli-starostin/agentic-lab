"""A manager's memory across conversations: Claude Code's own.

Claude Code keeps a memory folder (an index, MEMORY.md, and a file per memory), loads the
index into every session and lets the agent read and write it with its file tools. The lab
only decides whose memory a turn gets (for_turn):

- a dedicated manager: one memory, its member's, in their DMs and in their turns in channel
  threads (Claude Code's own folder for the manager's working folder);
- a shared manager: a folder per member (Claude Code's `autoMemoryDirectory`, set for each
  turn), so what one member says never reaches another;
- a guest's turn (another member on a dedicated manager): none (CLAUDE_CODE_DISABLE_AUTO_MEMORY).

The lab used to keep notes of its own, in its state folder, through `recall`/`remember`
tools, in DMs only: migrate() moves them into the member's memory once.
"""
import datetime as dt
import os
import re

OFF = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
MEMBER = re.compile(r"[a-z][a-z0-9_-]*")
NOTES = "lab-notes.md"


def default_dir(workdir, home=None):
    """Claude Code's memory folder for sessions in `workdir`: its project folder is the path
    with every character but letters and digits made '-'."""
    home = home or os.path.expanduser("~")
    return os.path.join(home, ".claude", "projects",
                        re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(workdir)), "memory")


class Memory:
    def __init__(self, root, shared, workdir, home=None):
        self.root = root                # the lab's old notes (migrate)
        self.shared = shared
        self.workdir = workdir
        self.home = home or os.path.expanduser("~")

    def folder(self, member):
        """The memory folder for `member`'s turns (None: no memory for them)."""
        if not self.shared:
            return default_dir(self.workdir, self.home)
        if not MEMBER.fullmatch(member or ""):
            return None
        return os.path.join(self.home, ".claude", "member-memory", member)

    def for_turn(self, member, guest=False):
        """(settings, env) for a turn on behalf of `member`."""
        if guest:
            return {}, dict(OFF)
        if not self.shared:
            return {}, {}
        d = self.folder(member)
        if d is None:
            return {}, dict(OFF)
        os.makedirs(d, mode=0o700, exist_ok=True)
        return {"autoMemoryDirectory": d}, {}

    def migrate(self, today=None):
        """Move the lab's old notes (notes.md; member-<id>.md on a shared manager) into the
        member's memory as lab-notes.md, with a line in its MEMORY.md. Each old file is kept
        as <name>.migrated. Returns the files moved."""
        try:
            names = sorted(os.listdir(self.root))
        except OSError:
            return []
        today = today or dt.date.today().isoformat()
        moved = []
        for n in names:
            if n == "notes.md" and not self.shared:
                member = None
            elif self.shared and n.startswith("member-") and n.endswith(".md"):
                member = n[len("member-"):-len(".md")]
            else:
                continue
            d = self.folder(member)
            if d is None:
                continue
            src = os.path.join(self.root, n)
            with open(src) as f:
                text = f.read().strip()
            if text:
                os.makedirs(d, mode=0o700, exist_ok=True)
                notes = os.path.join(d, NOTES)
                new = not os.path.exists(notes)
                with open(notes, "a") as f:
                    if new:
                        f.write("---\nname: lab-notes\ndescription: what was remembered with "
                                "the lab's old `remember` tool (projects, preferences, "
                                "decisions)\nmetadata:\n  type: project\n---\n\n")
                    f.write(f"Moved here {today} from the lab's old notes:\n\n{text}\n")
                os.chmod(notes, 0o600)
                index = os.path.join(d, "MEMORY.md")
                line = (f"- [Notes from the lab's old memory]({NOTES}) — what was remembered "
                        "with `remember` before " + today + "\n")
                have = open(index).read() if os.path.exists(index) else ""
                if f"]({NOTES})" not in have:
                    with open(index, "a") as f:
                        f.write(("" if not have or have.endswith("\n") else "\n") + line)
                    os.chmod(index, 0o600)
            os.replace(src, src + ".migrated")
            moved.append(n)
        return moved
