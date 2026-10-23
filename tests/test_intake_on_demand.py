"""The intake runs only while a one-time code is open, codes last 5 minutes, and links go
by DM only (#63)."""
import asyncio
import datetime as dt
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_lab import apply as A
from agentic_lab import registry
from agentic_lab.manager.state import ManagerState
from agentic_lab.secrets import intake as I

REPO = Path(__file__).parent.parent
EXAMPLE = REPO / "examples" / "minimal" / "lab.yaml"
SINGLE = REPO / "examples" / "single-vm" / "lab.yaml"


def test_a_code_opens_the_intake_for_five_minutes(tmp_path):
    intake = I.Intake(str(tmp_path))
    flag = tmp_path / I.OPEN_FLAG
    assert not flag.exists() and intake.close_if_idle()
    code = intake.request("alice", "claude-token")
    assert flag.exists() and intake.open_codes() == 1
    left = intake.expires(code) - dt.datetime.now(dt.timezone.utc)
    assert dt.timedelta(minutes=4) < left <= dt.timedelta(minutes=5)
    assert not intake.close_if_idle() and flag.exists()        # a code is open: keep serving
    intake.receive(code, "claude-token", "sk-ant-" + "x" * 60)
    assert intake.open_codes() == 0 and intake.close_if_idle() and not flag.exists()


def test_an_expired_code_closes_the_intake(tmp_path):
    intake = I.Intake(str(tmp_path))
    intake.request("alice", "claude-token", ttl=dt.timedelta(seconds=-1))
    assert intake.open_codes() == 0 and intake.close_if_idle()


def test_the_message_says_what_to_have_ready_and_for_how_long():
    until = dt.datetime(2026, 10, 22, 14, 5, tzinfo=dt.timezone.utc)
    msg = I.message("Alice", "K7QM-XP4D", "http://sec:8771", until=until)
    assert msg.index("Claude Code installed") < msg.index("curl -fsSL")
    assert "active for 5 minutes (until 14:05 UTC)" in msg and "ask again" in msg


# ── how it's deployed ────────────────────────────────────────────────────────

def test_the_intake_is_started_by_its_path_unit_and_hardened():
    svc = (REPO / "deploy/secretary/lab-intake.service").read_text()
    path = (REPO / "deploy/secretary/lab-intake.path").read_text()
    flag = os.path.join("/home/lab-secretary/.local/state/agentic-lab", I.OPEN_FLAG)
    assert f"PathExists={flag}" in path and "Unit=lab-intake.service" in path
    assert "--on-demand" in svc and "Restart=no" in svc and "[Install]" not in svc
    # a failed start takes the flag away: it isn't started again and again
    assert "ExecStopPost=/bin/sh -c '[ \"$$SERVICE_RESULT\" = success ] || rm -f " + flag in svc
    broker = (REPO / "deploy/secretary/lab-broker.service").read_text()
    for unit in (svc, broker):
        for line in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=tmpfs",
                     "PrivateTmp=yes", "PrivateDevices=yes", "CapabilityBoundingSet=",
                     "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
                     "SystemCallFilter=@system-service", "ProtectKernelTunables=yes",
                     "ProtectKernelModules=yes", "ProtectKernelLogs=yes",
                     "ProtectControlGroups=yes"):
            assert line in unit.splitlines(), line
    assert "BindPaths=/home/lab-secretary/.local/state/agentic-lab" in svc
    # other processes, the secretary's included, aren't visible to it
    assert "ProtectProc=invisible" in svc.splitlines()
    assert "BindReadOnlyPaths=/home/lab-broker/.config/agentic-lab" in broker


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="no systemd here")
def test_the_units_parse(tmp_path):
    for name in ("lab-intake.service", "lab-intake.path", "lab-broker.service"):
        text = (REPO / "deploy/secretary" / name).read_text()
        # verify the syntax only: the users and paths exist on the lab's host, not here
        text = "\n".join(ln for ln in text.splitlines()
                         if not ln.startswith(("User=", "Group=", "EnvironmentFile=")))
        (tmp_path / name).write_text(text)
    r = subprocess.run(["systemd-analyze", "verify", *[str(p) for p in tmp_path.iterdir()]],
                       capture_output=True, text=True)
    assert "Unknown key" not in r.stderr and "Unknown section" not in r.stderr, r.stderr


def test_apply_enables_the_path_unit_not_the_intake(tmp_path):
    from test_apply import fake_intake
    lab = registry.load(SINGLE)
    p = A.host_plan(lab, "dev-vm", A.Tokens(tmp_path), fake_intake({}))
    sh = A.script(p)
    assert "systemctl enable lab-intake.path >/dev/null 2>&1; systemctl restart lab-intake.path" in sh
    assert "install -m 644 /opt/agentic-lab/src/deploy/secretary/lab-intake.service" in sh
    assert "systemctl disable lab-intake.service >/dev/null 2>&1; systemctl try-restart " \
           "lab-intake.service" in sh
    assert "runuser -u lab-broker -- mkdir -p -m 700 /home/lab-broker/.local/state/agentic-lab" in sh
    assert subprocess.run(["bash", "-n"], input=sh, text=True).returncode == 0


# ── links only by DM ─────────────────────────────────────────────────────────

class Slack:
    def __init__(self):
        self.posts = []
        self.web = SimpleNamespace(conversations_open=self._open)

    async def _open(self, users):
        return {"channel": {"id": f"D-{users}"}}

    async def post(self, channel, thread_ts, text):
        self.posts.append((channel, text))


def secretary(tmp_path, monkeypatch):
    from agentic_lab.secretary.service import Secretary
    monkeypatch.setenv("HOME", str(tmp_path))
    sec = Secretary(registry.load(EXAMPLE), {"state": None}, None, None,
                    ManagerState(str(tmp_path / "chat")), slack=Slack())
    sec.intake = I.Intake(str(tmp_path / "intake"))

    async def up(url, wait_s=15):
        return True
    monkeypatch.setattr("agentic_lab.secretary.service.intake_up", up)
    return sec


def test_links_asked_for_in_a_channel_go_by_dm(tmp_path, monkeypatch):
    sec = secretary(tmp_path, monkeypatch)
    channel = SimpleNamespace(channel="C-LAB", thread_ts="1.0")
    said = asyncio.run(sec.request_github_token("flash-bot", "alice", channel))
    assert all(ch == "D-U00000001" for ch, _ in sec.slack.posts) and sec.slack.posts
    assert "DM" in said and sec.intake.open_codes() == 0        # steps first, no code yet
    said = asyncio.run(sec.request_github_token("flash-bot", "alice", channel, ready=True))
    ch, text = sec.slack.posts[-1]
    assert ch == "D-U00000001" and "/s/" in text and "active for 5 minutes" in text
    assert sec.intake.open_codes() == 1


def test_a_members_token_command_goes_to_them_when_they_ask(tmp_path, monkeypatch):
    sec = secretary(tmp_path, monkeypatch)
    sec.lab.humans["bob"] = {"name": "Bob", "slack_user": "U-BOB"}
    asyncio.run(sec.request_token("bob", asked_by="alice"))       # an admin asks for it
    ch, text = sec.slack.posts[-1]
    assert ch == "D-U-BOB" and "lab-member.py" not in text and "send my token command" in text
    assert sec.intake.open_codes() == 0
    asyncio.run(sec.request_token("bob", asked_by="bob"))         # bob, ready
    ch, text = sec.slack.posts[-1]
    assert ch == "D-U-BOB" and "lab-member.py" in text and sec.intake.open_codes() == 1


def test_a_failed_intake_is_said_not_a_dead_link(tmp_path, monkeypatch):
    sec = secretary(tmp_path, monkeypatch)

    async def down(url, wait_s=15):
        return False
    monkeypatch.setattr("agentic_lab.secretary.service.intake_up", down)
    said = asyncio.run(sec.request_github_token("flash-bot", "alice",
                                                SimpleNamespace(channel="C", thread_ts=None),
                                                ready=True))
    assert "failed to start" in said and not any("/s/" in t for _, t in sec.slack.posts)


def test_bad_codes_are_counted_across_starts(tmp_path):
    intake = I.Intake(str(tmp_path))
    for _ in range(I.MAX_FAILURES):
        with pytest.raises(I.IntakeError):
            intake.receive("AAAA-BBBB", "claude-token", "x", source="100.1.2.3")
    again = I.Intake(str(tmp_path))                  # started anew for the next code
    with pytest.raises(I.IntakeError, match="too many"):
        again.receive("AAAA-BBBB", "claude-token", "x", source="100.1.2.3")
