"""What lab.yaml and agents provide is handled as data: quoted, validated, shown as is."""
import asyncio
import datetime as dt
import subprocess
from pathlib import Path

import pytest
import yaml

from agentic_lab import apply as A
from agentic_lab import registry
from agentic_lab.broker import core as C
from agentic_lab.broker.slack import _grant_text
from agentic_lab.policy import grants as G
from agentic_lab.secretary.home import _inert
from agentic_lab.worker import tasks as T
from agentic_lab.worker.runner import MAX_SESSION_RESTARTS, SdkRunner

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"


@pytest.mark.parametrize("workdir", ["~/x/y'; true; echo '", "~/a b", "~/../x", "~/$(x)",
                                     "~/a\nb"])
def test_working_folders_are_plain_paths(workdir, tmp_path):
    d = yaml.safe_load(EXAMPLE.read_text())
    d["agents"]["worker-a"]["workdir"] = workdir
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(d))
    errs = [str(i) for i in registry.check(registry.load(p)) if i.level == "error"]
    assert any("workdir" in e for e in errs)


def test_messages_in_the_deploy_script_are_quoted():
    lines = A.readers_script(["alice"], {"lab-w": "/home/lab-w/deep/work'x"})
    line = next(ln for ln in lines if "not directly" in ln)
    r = subprocess.run(["bash", "-c", line], capture_output=True, text=True)
    assert r.returncode == 0 and "deep/work'x" in r.stdout


def test_the_readers_step_never_stops_the_deploy():
    lines = A.readers_script(["alice"], {"lab-w": "/home/lab-w/work"})
    for ln in lines:
        if "runuser" in ln and ("chmod" in ln or "setfacl -R" in ln):
            assert "||" in ln, ln


def test_owner_files_are_read_as_the_owner():
    s = A.self_admin_script({"config_owner": "lab-secretary", "config_url": "u",
                             "lab_rel": "lab.yaml", "framework_source": "/s",
                             "framework_owner": "admin"})
    line = next(ln for ln in s if "config-repo-key.pub" in ln and "sed" in ln)
    assert "runuser -u lab-secretary -- sed" in line


@pytest.mark.parametrize("hidden", ["ㅤ", "ᅟ", "ᅠ", "ﾠ", "⠀", "͏",
                                    "​", " "])
def test_nothing_shown_can_hide_a_character(hidden):
    assert hidden not in C.describe("Bash", {"command": f"bash setup.sh{hidden}"})
    assert hidden not in C.describe("Write", {"file_path": f"/w/a{hidden}.sh",
                                              "content": f"x{hidden}y"})
    assert hidden not in C.visible(f"a{hidden}b")


def test_text_stays_readable():
    d = C.describe("Write", {"file_path": "/w/notes.md", "content": "Привет, мир — ok"})
    assert "Привет, мир — ok" in d
    assert "\\u" in C.describe("Bash", {"command": "echo Привет"})    # commands: ASCII
    assert "\\u" in C.describe("WebFetch", {"url": "https://exаmple.org"})
    assert "\\u" in _grant_text([{"kind": "domain", "value": "exаmple.org"}])


def test_the_home_tab_shows_details_inert():
    t = _inert("`x` <!here> *b* <https://a.org|ok>")
    assert "`" not in t and "<" not in t and "*" not in t


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit", "Read"])
def test_file_tools_are_granted_by_path_only(tool):
    with pytest.raises(G.GrantError):
        G.validate("tool", tool)


@pytest.mark.parametrize("path", ["~/.config/agentic-lab", "~/.local/state/agentic-lab/x",
                                  "~/.ssh", "/home/lab-w/.config/x", "~/data/../.config",
                                  "//home/lab-w/.ssh", "///home//lab-w/x", "~//.config",
                                  "~/./.config/", "~/.claude", "~/.bashrc", "~/.gitconfig",
                                  "~lab-other/data", "/home/lab-other/work", "/", "//", "~/"])
def test_write_grants_stay_out_of_the_services_folders(path):
    with pytest.raises(G.GrantError):
        G.validate("write", path)


def test_write_grants_elsewhere_are_fine():
    G.validate("write", "~/datasets")
    G.validate("write", "/data/shared")


def test_restarts_for_expiry_are_bounded(tmp_path, monkeypatch):
    store = T.TaskStore(tmp_path / "tasks")
    t = store.update(store.submit("x", "alice", "m").id, state=T.RUNNING)
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat()

    class Approver:
        async def active_grants(self):
            return [{"kind": "domain", "value": "a.example.org", "expires": soon},
                    {"kind": "domain", "value": "b.example.org", "expires": past}]
    r = SdkRunner("w", str(tmp_path), lambda g: {}, approver=Approver())
    sessions = []

    async def session(task, store_, outcome, sess, grants, prompt):
        sessions.append([g["value"] for g in grants])
        sess["expired"] = True              # every session cut short by an expiry
        return None, None
    monkeypatch.setattr(r, "_session", session)
    asyncio.run(r.run(t, store))
    assert len(sessions) == MAX_SESSION_RESTARTS + 1
    assert all(s == ["a.example.org"] for s in sessions)       # the past one isn't used


@pytest.mark.parametrize("workdir", ["~/.config", "~/.ssh/work", "~/", "/home/lab-w",
                                     "/home/lab-w/.local/x"])
def test_working_folders_arent_settings_folders(workdir, tmp_path):
    d = yaml.safe_load(EXAMPLE.read_text())
    d["agents"]["worker-a"]["workdir"] = workdir
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(d))
    errs = [str(i) for i in registry.check(registry.load(p)) if i.level == "error"]
    assert any("workdir" in e for e in errs)


@pytest.mark.parametrize("hidden", ["\U0001d159", "ᆣ", "￼", " "])
def test_more_characters_that_show_as_nothing(hidden):
    d = C.describe("Write", {"file_path": "/w/a.sh", "content": f"x{hidden}y"})
    assert hidden not in d and f"\\u{{{ord(hidden):x}}}" in d


def test_file_content_with_look_alikes_is_flagged():
    d = C.describe("Write", {"file_path": "/w/a.sh", "content": "curl https://exаmple.org"})
    assert "non-ASCII" in d and "\\u{430}" in d
    assert "non-ASCII" not in C.describe("Write", {"file_path": "/w/a.sh", "content": "ok"})
