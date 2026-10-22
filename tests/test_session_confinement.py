"""The agent's own session starts with the lab's folders covered, and every way the agent
runs anything hides the same core (sandbox.lab_own), plus only its documented extras."""
import os

import pytest

from agentic_lab.policy import confine as CF
from agentic_lab.policy import sandbox
from agentic_lab.policy import session as S
from agentic_lab.worker import runs as R


def home(tmp_path):
    h = tmp_path / "home"
    for d in (".config/agentic-lab", ".local/state/agentic-lab", ".nebius",
              ".local/share/agentic-lab/github/bot", ".claude", "work/.cache"):
        (h / d).mkdir(parents=True)
    return str(h)


def tmpfs(argv):
    return [argv[i + 1] for i in range(len(argv)) if argv[i] == "--tmpfs"]


def test_every_path_hides_the_same_core_plus_only_its_own_extras(tmp_path, monkeypatch):
    h = home(tmp_path)
    monkeypatch.setenv("HOME", h)
    core = sandbox.lab_own(h)
    assert core == [f"{h}/.config/agentic-lab", f"{h}/.local/state/agentic-lab",
                    f"{h}/.nebius"]
    session = tmpfs(S.wrapper(h, "/c/claude"))
    fs = sandbox.settings(f"{h}/work", gpu=True)["sandbox"]["filesystem"]
    commands = fs["denyRead"]
    run = tmpfs(R.job_wrapper(fs, f"{h}/work"))
    approved = tmpfs(CF.confined("true", h).split())
    for hidden in (session, commands, run, approved):
        assert [d for d in hidden if d in core] == core
    # the extras, each with its reason beside it in the code
    assert [d for d in session if d not in core] == []
    assert [d for d in commands if d not in core] == sandbox.HIDDEN           # sockets
    assert {d for d in run if d not in core} == {os.path.realpath("/run")}  # the same, resolved
    assert [d for d in approved if d not in core] == \
        [f"{h}/.local/share/agentic-lab/github/bot"]                        # #76


def test_the_session_keeps_everything_else_as_it_is(tmp_path):
    h = home(tmp_path)
    w = S.wrapper(h, "/c/claude")
    assert w[:5] == ["/usr/bin/bwrap", "--die-with-parent", "--dev-bind", "/", "/"]
    assert w[-2:] == ["--", "/c/claude"]
    assert "/run" not in w                                 # the resolver, approved commands
    assert not any(a.startswith("--ro") for a in w)        # nothing made read-only
    for kept in (f"{h}/.claude", f"{h}/work", f"{h}/work/.cache", "/tmp",
                 f"{h}/.local/share/agentic-lab/github"):
        assert kept not in tmpfs(w)
    assert "--unsetenv" not in w and "--clearenv" not in w  # claude needs its seat token
    assert "--unshare-pid" not in w                         # the SDK talks to it as usual
    binds = [w[i + 1] for i in range(len(w)) if w[i] == "--bind"]
    assert set(binds) == {f"{h}/.config", f"{h}/.local", f"{h}/.local/state"}


def test_no_bwrap_no_session(tmp_path, monkeypatch):
    launcher = tmp_path / "lab-claude"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    assert S.resolve(str(launcher), "/usr/bin/true") == str(launcher)
    with pytest.raises(S.SessionError, match="can't be confined"):
        S.resolve(str(launcher), str(tmp_path / "no-bwrap"))
    with pytest.raises(S.SessionError):
        S.resolve(str(tmp_path / "none"), "/usr/bin/true")
    monkeypatch.setattr(S, "REFUSE_UNCONFINED", False)     # the one switch
    assert S.resolve(str(launcher), str(tmp_path / "no-bwrap")) is None


def test_the_launcher_takes_nothing_from_the_environment(tmp_path, monkeypatch):
    import pwd
    h = home(tmp_path)
    ran = []
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: type("P", (), {"pw_dir": h}))
    monkeypatch.setattr(S, "claude_cli", lambda: "/c/claude")
    monkeypatch.setattr(S.os, "access", lambda p, m: True)
    monkeypatch.setattr(S.os, "execv", lambda path, argv: ran.append((path, argv)))
    monkeypatch.setattr(S.sys, "argv", ["lab-claude", "--output-format", "stream-json"])
    S.main()
    for name, value in (("HOME", "/elsewhere"), ("PATH", str(tmp_path)),
                        ("CLAUDE_CONFIG_DIR", "/x"), ("XDG_CONFIG_HOME", "/x")):
        monkeypatch.setenv(name, value)
    S.main()
    assert ran[0] == ran[1] == ("/usr/bin/bwrap", S.wrapper(h, "/c/claude")
                                + ["--output-format", "stream-json"])


def test_the_bundled_claude_or_none(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    c = S.claude_cli()
    assert c is None or c.endswith("/_bundled/claude")     # never one from the PATH


def test_a_session_is_started_through_the_launcher(tmp_path, monkeypatch):
    from agentic_lab.worker import tasks as T
    from agentic_lab.worker.runner import SdkRunner
    r = SdkRunner("w", str(tmp_path), lambda g: {})
    store = T.TaskStore(tmp_path / "tasks")
    t = store.submit("x", "alice", "m")
    monkeypatch.setattr(S, "cli_path", lambda: "/opt/agentic-lab/venv/bin/lab-claude")
    opts = r._options(t, store, {}, {"violations": [], "restart_for": None}, [])
    assert opts.cli_path == "/opt/agentic-lab/venv/bin/lab-claude"

    def refuse():
        raise S.SessionError("no bwrap")
    monkeypatch.setattr(S, "cli_path", refuse)
    with pytest.raises(S.SessionError):
        r._options(t, store, {}, {"violations": [], "restart_for": None}, [])
