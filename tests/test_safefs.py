"""Services reading and writing in folders their agents can change stay inside them."""
import base64
from types import SimpleNamespace

import pytest

from agentic_lab import safefs
from agentic_lab.manager import tools as MT
from agentic_lab.worker import runs as R
from agentic_lab.worker.server import ToolError, read_workdir_file, write_inbox_file


@pytest.fixture
def folders(tmp_path):
    work, other = tmp_path / "work", tmp_path / "other"
    work.mkdir()
    other.mkdir()
    (other / "kept").write_text("KEPT")
    (work / "plain").write_text("hello\n")
    return work, other


def test_plain_files_inside_are_read(folders):
    work, _ = folders
    assert safefs.read(work, "plain") == b"hello\n"
    assert safefs.read(work, str(work / "plain")) == b"hello\n"
    assert safefs.tail(work, "plain", 3) == b"lo\n"


def test_only_plain_files_inside_the_folder(folders):
    work, other = folders
    (work / "alias").symlink_to(other / "kept")
    (work / "sub").symlink_to(other)
    for path in ("alias", "sub/kept", "../other/kept", str(other / "kept"), "."):
        with pytest.raises(OSError):
            safefs.read(work, path)


def test_writes_replace_never_follow(folders):
    work, other = folders
    (work / "alias").symlink_to(other / "kept")
    safefs.write(work, "alias", b"new")
    assert (other / "kept").read_text() == "KEPT"
    assert (work / "alias").read_bytes() == b"new" and not (work / "alias").is_symlink()
    (work / "sub").symlink_to(other)
    with pytest.raises(OSError):
        safefs.write(work, "sub/x", b"new")
    assert not (other / "x").exists()
    assert safefs.write(work, "a/b/c", b"deep").endswith("work/a/b/c")


def test_appends_never_follow(folders):
    work, other = folders
    (work / "log").symlink_to(other / "kept")
    with pytest.raises(OSError):
        safefs.open_append(work, "log")
    assert (other / "kept").read_text() == "KEPT"


def test_a_runs_log_tail_shows_only_its_log(tmp_path, folders):
    work, other = folders
    runs = R.RunStore(str(tmp_path / "state"), str(work), "worker-a",
                      systemctl=lambda *a: True)
    r = runs.start("t-1", "x", "true")
    log = r["log"]
    open(log, "w").write("line one\n")
    assert runs.view(r["id"], tail=5)["log_tail"] == "line one"
    import os
    os.unlink(log)
    os.symlink(other / "kept", log)
    assert "KEPT" not in runs.view(r["id"], tail=5)["log_tail"]


def test_the_worker_hands_out_and_stores_only_plain_files(folders):
    work, other = folders
    assert base64.b64decode(read_workdir_file(str(work), "plain")["base64"]) == b"hello\n"
    (work / "alias").symlink_to(other / "kept")
    with pytest.raises(ToolError):
        read_workdir_file(str(work), "alias")
    (work / "inbox").symlink_to(other)
    with pytest.raises(ToolError):
        write_inbox_file(str(work), "kept", base64.b64encode(b"new").decode())
    assert (other / "kept").read_text() == "KEPT"


def test_the_manager_posts_only_its_own_plain_files(folders):
    work, other = folders
    ctx = SimpleNamespace(workdir=str(work))
    assert MT._read_own(ctx, "plain") == b"hello\n"
    (work / "alias").symlink_to(other / "kept")
    assert MT._read_own(ctx, "alias") is None and MT._read_own(ctx, "../other/kept") is None


def test_the_folder_stays_the_one_pinned(tmp_path, folders):
    work, other = folders
    safefs.pin(work)                        # at the service's start
    moved = tmp_path / "moved"
    work.rename(moved)
    work.symlink_to(other)
    with pytest.raises(OSError):
        safefs.read(work, "kept")


def test_a_working_folder_on_another_disk_works(tmp_path):
    disk = tmp_path / "nvme" / "work"
    disk.mkdir(parents=True)
    (disk / "result.txt").write_text("ok")
    work = tmp_path / "home" / "work"
    work.parent.mkdir()
    work.symlink_to(disk)                   # e.g. ~/work -> /mnt/nvme/work
    safefs.pin(work)
    assert safefs.read(work, "result.txt") == b"ok"
    assert safefs.read(work, str(work / "result.txt")) == b"ok"      # either form
    assert safefs.read(work, str(disk / "result.txt")) == b"ok"
    assert safefs.write(work, "inbox/a.txt", b"x").endswith("home/work/inbox/a.txt")
    assert (disk / "inbox" / "a.txt").read_bytes() == b"x"
    import os
    os.close(safefs.open_append(work, "runs/r/log"))
    assert (disk / "runs" / "r" / "log").exists()
    b64 = read_workdir_file(str(work), "result.txt")["base64"]
    assert base64.b64decode(b64) == b"ok"


def test_only_files_with_a_single_name(folders):
    import os
    work, other = folders
    os.link(other / "kept", work / "second")
    with pytest.raises(OSError):
        safefs.read(work, "second")
    with pytest.raises(OSError):
        safefs.open_append(work, "second")


def test_special_files_are_never_waited_on(folders):
    import os
    import threading
    work, _ = folders
    os.mkfifo(work / "log")
    done = []
    t = threading.Thread(target=lambda: done.append(
        pytest.raises(OSError, safefs.open_append, work, "log")), daemon=True)
    t.start()
    t.join(2)
    assert done, "opening it blocked"


def test_the_secretary_posts_the_icon_it_drew(monkeypatch):
    import asyncio
    from agentic_lab.secretary.service import Secretary
    sent = []

    async def drawn(name, role, env=None, lab=None):
        return b"PNG", "drawn"

    class Slack:
        async def upload(self, channel, thread_ts, path, title, comment=None, data=None):
            sent.append((path, data))
    monkeypatch.setattr("agentic_lab.icon.drawn", drawn)
    me = SimpleNamespace(brain=None, lab=SimpleNamespace(name="L"), slack=Slack())
    asyncio.run(Secretary.post_icon(me, "X", "manager", SimpleNamespace(channel="C",
                                                                         thread_ts=None), "c"))
    assert sent == [(None, b"PNG")]
