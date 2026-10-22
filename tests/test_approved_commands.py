"""An approved command runs as approved, without the lab's own credentials and state."""
import asyncio
import shlex

from agentic_lab.policy import confine as CF
from agentic_lab.worker import tasks as T
from agentic_lab.worker.runner import Decision, SdkRunner

CMD = """cd /data && tar czf "out dir.tgz" 'a b' && echo "$HOME" | sed 's/x/y/'"""


def home(tmp_path):
    h = tmp_path / "home"
    for d in (".config/agentic-lab", ".local/state/agentic-lab",
              ".local/share/agentic-lab/github/bot/"):
        (h / d).mkdir(parents=True)
    (h / ".local/share/agentic-lab/github/bot/token").write_text("t")
    (h / ".local/share/agentic-lab/github/bot/gitconfig").write_text("[user]")
    return str(h)


def test_the_command_itself_is_unchanged(tmp_path):
    argv = shlex.split(CF.confined(CMD, home(tmp_path)))
    assert argv[-3:] == ["/bin/bash", "-c", CMD]


def test_only_the_labs_own_things_are_taken_away(tmp_path):
    h = home(tmp_path)
    argv = shlex.split(CF.confined(CMD, h))
    assert argv[:8] == ["/usr/bin/bwrap", "--die-with-parent", "--unshare-pid",
                        "--dev-bind", "/", "/", "--proc", "/proc"]
    opts = argv[8:argv.index("--")]
    pairs = [(opts[i], opts[i + 1]) for i in range(len(opts)) if opts[i] == "--tmpfs"]
    assert {d for _, d in pairs} == {f"{h}/.config/agentic-lab",
                                     f"{h}/.local/state/agentic-lab",
                                     f"{h}/.local/share/agentic-lab/github/bot"}
    assert opts[-2:] == ["--unsetenv", "CLAUDE_CODE_OAUTH_TOKEN"]
    # nothing else: the machine, files, network and environment as approved
    assert {o for o in opts if o.startswith("--")} == {"--bind", "--tmpfs", "--unsetenv"}
    assert "gitconfig" not in " ".join(opts)


def test_what_holds_the_covered_folders_stays_in_place(tmp_path):
    """Every folder above a covered one, up to the home, is bound onto itself (it stays
    writable), before the covering, so none of them can be moved."""
    h = home(tmp_path)
    argv = shlex.split(CF.confined(CMD, h))
    opts = argv[8:argv.index("--")]
    binds = [opts[i + 1] for i in range(len(opts)) if opts[i] == "--bind"]
    for b in binds:
        i = opts.index(b)
        assert opts[i - 1] == "--bind" and opts[i + 1] == b          # onto itself
    assert set(binds) == {f"{h}/.config", f"{h}/.local", f"{h}/.local/state",
                          f"{h}/.local/share", f"{h}/.local/share/agentic-lab",
                          f"{h}/.local/share/agentic-lab/github"}
    assert max(i for i, o in enumerate(opts) if o == "--bind") < \
        min(i for i, o in enumerate(opts) if o == "--tmpfs")
    assert h not in binds                                            # the home: as it is


def test_only_commands_that_leave_the_sandbox_change():
    assert CF.approved_input("Bash", {"command": "ls"}) is None
    assert CF.approved_input("WebFetch", {"url": "https://a.org"}) is None
    out = CF.approved_input("Bash", {"command": "ls", "dangerouslyDisableSandbox": True,
                                     "description": "list"})
    assert out["dangerouslyDisableSandbox"] and out["description"] == "list"
    assert shlex.split(out["command"])[-1] == "ls"


def test_the_human_decides_on_the_command_as_asked(tmp_path):
    store = T.TaskStore(tmp_path / "tasks")
    t = store.update(store.submit("x", "alice", "m").id, state=T.RUNNING)
    asked = []

    class Approver:
        async def ask(self, task, tool, inp, violations, stop=None, filed=None):
            asked.append(dict(inp))
            return Decision("allow")

        async def active_grants(self):
            return []
    r = SdkRunner("w", str(tmp_path), lambda g: {}, approver=Approver())
    opts = r._options(t, store, {}, {"violations": [], "restart_for": None}, [])
    inp = {"command": CMD, "dangerouslyDisableSandbox": True}
    res = asyncio.run(opts.can_use_tool("Bash", inp, None))
    assert asked == [inp]                                       # what the human saw
    assert shlex.split(res.updated_input["command"])[-1] == CMD   # what then runs
    assert res.updated_input["command"].startswith("/usr/bin/bwrap ")
