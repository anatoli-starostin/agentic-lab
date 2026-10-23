"""A member's own approval timeout, set from Slack through the secretary (#77). A timeout
only ever denies: a request past its deadline is a timeout, and can't be decided after."""
import asyncio
import datetime as dt
from types import SimpleNamespace

import httpx2
import pytest

from agentic_lab.broker import core as C
from agentic_lab.broker.http import app

HUMANS = {"alice": {"slack_user": "U-ALICE", "timeout_s": 120},
          "bob": {"slack_user": "U-BOB"}}
AGENTS = {"sec": {"role": "secretary"}, "worker-a": {"role": "worker"}}
BASH = {"command": "curl https://example.org", "dangerouslyDisableSandbox": True}


def broker(tmp_path):
    return C.Broker(tmp_path, HUMANS, AGENTS)


def test_lab_yaml_is_the_default_and_an_override_wins(tmp_path):
    b = broker(tmp_path)
    assert b.timeout_for("alice") == (120, "lab.yaml")
    assert b.timeout_for("bob") == (None, "lab.yaml")
    assert b.set_timeout("bob", 900, by="bob") == (900, "set by bob")
    assert b.set_timeout("alice", None, by="alice")[0] is None          # "none": no timeout
    # it survives the broker's restart (and so a deploy)
    assert broker(tmp_path).timeout_for("bob") == (900, "set by bob")
    assert b.set_timeout("bob", "default", by="bob") == (None, "lab.yaml")


@pytest.mark.parametrize("value", [59, 7 * 86400 + 1, 0, -5, "15", True, 1.5])
def test_out_of_bounds_values_are_refused(tmp_path, value):
    with pytest.raises(C.BrokerError, match="1 minute to 7 days"):
        broker(tmp_path).set_timeout("bob", value, by="bob")


def test_an_override_with_an_end_reverts_to_the_default(tmp_path, monkeypatch):
    b = broker(tmp_path)
    t0 = dt.datetime(2026, 10, 23, 9, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(C, "now", lambda: t0)
    b.set_timeout("alice", 600, by="alice", until="2026-10-23T18:00:00")    # naive: UTC
    assert b.timeout_for("alice")[0] == 600
    monkeypatch.setattr(C, "now", lambda: t0 + dt.timedelta(hours=9))
    assert b.timeout_for("alice") == (120, "lab.yaml")
    with pytest.raises(C.BrokerError, match="already past"):
        b.set_timeout("alice", 600, by="alice", until="2026-10-23T17:00:00+00:00")


def test_new_requests_follow_it_pending_ones_keep_their_deadline(tmp_path):
    b = broker(tmp_path)
    before = b.submit("worker-a", "bob", "Bash", BASH)
    assert before.expires is None
    b.set_timeout("bob", 300, by="bob")
    after = b.submit("worker-a", "bob", "Bash", BASH)
    assert after.expires is not None and b.get(before.id).expires is None


def test_a_timed_out_request_is_never_approved(tmp_path, monkeypatch):
    b = broker(tmp_path)
    b.set_timeout("bob", 60, by="bob")
    req = b.submit("worker-a", "bob", "Bash", BASH)
    late = dt.datetime.fromisoformat(req.expires) + dt.timedelta(seconds=1)
    monkeypatch.setattr(C, "now", lambda: late)
    # a click after the deadline, before the expiry sweep: a timeout, not a yes
    with pytest.raises(C.BrokerError, match="timed out before your answer"):
        b.decide(req.id, "once", "U-BOB")
    r = b.get(req.id)
    assert r.state == C.DECIDED and r.decision["action"] == "timeout"
    with pytest.raises(C.BrokerError, match="already decided"):
        b.decide(req.id, "once", "U-BOB")
    assert b.grants("worker-a") == []


def test_only_the_secretary_reads_or_changes_them(tmp_path, free_port):
    import threading
    import time

    import uvicorn
    b = broker(tmp_path)
    server = uvicorn.Server(uvicorn.Config(app(b, {"s" * 40: "sec", "w" * 40: "worker-a"}),
                                           host="127.0.0.1", port=free_port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.02)
    url = f"http://127.0.0.1:{free_port}/v1/timeouts"
    try:
        worker = {"Authorization": "Bearer " + "w" * 40}
        assert httpx2.post(url, headers=worker, json={"human": "bob", "timeout_s": 600,
                                                      "by": "x"}).status_code == 403
        sec = {"Authorization": "Bearer " + "s" * 40}
        r = httpx2.post(url, headers=sec, json={"human": "bob", "timeout_s": 600, "by": "bob"})
        assert r.json()["bob"] == {"timeout_s": 600, "source": "set by bob"}
        assert httpx2.post(url, headers=sec, json={"human": "bob", "timeout_s": 5,
                                                   "by": "bob"}).status_code == 400
    finally:
        server.should_exit = True
        th.join()


def secretary(events):
    from agentic_lab.secretary.service import Secretary
    s = Secretary.__new__(Secretary)
    s.lab = SimpleNamespace(humans={"alice": {"admin": True}, "bob": {}, "carol": {}})
    s.events = events
    return s


def test_a_member_changes_their_own_an_admin_anyones():
    sent, logged = [], []
    events = SimpleNamespace(add=lambda kind, text="", **m: logged.append((kind, text)))
    s = secretary(events)

    async def call(body):
        sent.append(body)
        return {"bob": {"timeout_s": 900, "source": "set by bob"}}

    async def tz_of(member):
        return None

    def ask(member, minutes, asked_by, until=None):
        return asyncio.run(s.approval_timeout(member, minutes, until, asked_by, call=call,
                                              tz_of=tz_of))
    assert "refused" in ask("carol", "15", "bob") and sent == []
    assert "15 minutes" in ask("bob", "15", "bob")
    assert sent[-1] == {"human": "bob", "by": "bob", "until": None, "timeout_s": 900}
    ask("bob", "none", "alice")                                  # an admin, someone else's
    assert sent[-1]["timeout_s"] is None and sent[-1]["by"] == "alice"
    ask("bob", "default", "bob")
    assert sent[-1]["timeout_s"] == "default"
    assert "number" in ask("bob", "soon", "bob")
    assert ask("bob", None, "carol").startswith("bob's approval timeout")    # reading: anyone
    assert [k for k, _ in logged] == ["approval_timeout"] * 3


def test_until_is_the_members_own_time_shown_in_both():
    from zoneinfo import ZoneInfo
    sent = []
    s = secretary(None)

    async def call(body):
        sent.append(body)
        return {"bob": {"timeout_s": 600, "source": f"set by bob until {body['until']}"}}

    async def berlin(member):
        return ZoneInfo("Europe/Berlin")

    async def unknown(member):
        return None
    out = asyncio.run(s.approval_timeout("bob", "10", "2026-10-23T18:00", "bob", call=call,
                                         tz_of=berlin))
    assert sent[-1]["until"] == "2026-10-23T16:00:00+00:00"          # 18:00 CEST is 16:00 UTC
    assert "until 2026-10-23 18:00 CEST (16:00 UTC)" in out
    out = asyncio.run(s.approval_timeout("bob", "10", "2026-10-23T18:00", "bob", call=call,
                                         tz_of=unknown))
    assert sent[-1]["until"] == "2026-10-23T18:00:00+00:00" and "isn't known" in out
    assert "goes with" in asyncio.run(s.approval_timeout("bob", None, "2026-10-23T18:00",
                                                         "bob", call=call, tz_of=berlin))


def test_a_late_click_is_told_it_timed_out(tmp_path, monkeypatch):
    from agentic_lab.broker.slack import SlackChannel
    b = broker(tmp_path)
    b.set_timeout("bob", 60, by="bob")
    req = b.submit("worker-a", "bob", "Bash", BASH)
    monkeypatch.setattr(C, "now", lambda: dt.datetime.fromisoformat(req.expires)
                        + dt.timedelta(seconds=1))
    told = []

    class Web:
        async def chat_postEphemeral(self, **kw):
            told.append(kw["text"])

    async def click():
        ch = SlackChannel.__new__(SlackChannel)
        ch.broker, ch.web, ch.loop = b, Web(), asyncio.get_running_loop()
        ch._decide(req.id, "once", "U-BOB", None, {"channel": {"id": "D-BOB"}})
        await asyncio.sleep(0.05)
    asyncio.run(click())
    assert told == ["Not applied: it timed out before your answer, so it was denied"]
    assert b.get(req.id).decision["action"] == "timeout"


def test_an_unreadable_overrides_file_is_said_and_falls_back(tmp_path, caplog):
    b = broker(tmp_path)
    (tmp_path / "timeouts.json").write_text("{not json")
    assert b.timeout_for("alice") == (120, "lab.yaml")
    assert "unreadable" in caplog.text
