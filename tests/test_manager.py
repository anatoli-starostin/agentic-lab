"""The manager: broker integration, memory, visibility, Slack routing, follow-ups, manifests."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_lab import registry, slack_manifest
from agentic_lab.broker import core as C
from agentic_lab.manager import followup
from agentic_lab.manager.memory import Memory
from agentic_lab.manager.service import compose
from agentic_lab.manager.state import ManagerState
from agentic_lab.manager.tools import visible
from agentic_lab.slackio import SlackIO

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"
HUMANS = {"alice": {"slack_user": "U-ALICE", "timeout_s": 60},
          "bob": {"slack_user": "U-BOB", "timeout_s": 60}}
AGENTS = {"mgr-alice": {"role": "manager", "human": "alice", "access": "dedicated"},
          "mgr-team": {"role": "manager", "human": "alice", "access": "shared"},
          "worker-a": {"role": "worker", "human": "alice", "access": "shared"},
          "worker-b": {"role": "worker", "human": "alice", "access": "shared"}}
CURL = {"command": "curl https://arxiv.org/x", "dangerouslyDisableSandbox": True}


@pytest.fixture
def broker(tmp_path):
    return C.Broker(tmp_path, HUMANS, AGENTS)


# ── broker: who a worker's request is for ────────────────────────────────────

def test_registered_requester_wins(broker):
    broker.register_task("mgr-team", "worker-a", "t-1", "bob")
    r = broker.submit("worker-a", "alice", "Bash", CURL, task_id="t-1")   # worker claims alice
    assert r.human == "bob" and r.manager == "mgr-team" and r.requester_confirmed
    with pytest.raises(C.BrokerError, match="isn't worker-b's"):
        broker.submit("worker-b", "bob", "Bash", CURL, task_id="t-1")
    with pytest.raises(C.BrokerError, match="already registered"):
        broker.register_task("mgr-team", "worker-a", "t-1", "alice")


def test_unregistered_task_is_unconfirmed(broker):
    r = broker.submit("worker-a", "alice", "Bash", CURL, task_id="t-9")
    assert r.human == "alice" and not r.requester_confirmed and r.manager is None


def test_dedicated_manager_serves_its_member_only(broker):
    with pytest.raises(C.BrokerError, match="dedicated to alice"):
        broker.register_task("mgr-alice", "worker-a", "t-2", "bob")
    with pytest.raises(C.BrokerError, match="dedicated to alice"):
        broker.submit("mgr-alice", "bob", "Write", {"file_path": "/x"}, role="manager")
    r = broker.submit("mgr-team", "bob", "Write", {"file_path": "/repo/x"}, role="manager")
    assert r.human == "bob" and r.manager == "mgr-team"


def test_managers_see_and_annotate_their_requests_only(broker):
    broker.register_task("mgr-team", "worker-a", "t-1", "bob")
    mine = broker.submit("worker-a", "x", "Bash", CURL, task_id="t-1")
    other = broker.submit("worker-b", "alice", "Bash", CURL, task_id="t-8")
    assert [r.id for r in broker.for_manager("mgr-team")] == [mine.id]
    broker.add_context(mine.id, "needed for the eval", manager="mgr-team")
    with pytest.raises(C.BrokerError, match="no request"):
        broker.add_context(other.id, "sneaky", manager="mgr-team")


# ── memory and visibility ────────────────────────────────────────────────────

def test_memory_dedicated_shared_and_threads(tmp_path):
    shared = Memory(tmp_path / "s", shared=True)
    shared.remember("alice", True, "works on LUT pooling")
    assert "LUT pooling" in shared.recall("alice", True)
    assert "LUT pooling" not in shared.recall("bob", True)             # per member
    assert "no memory in channel threads" in shared.recall("alice", False)
    assert "not remembered" in shared.remember("alice", False, "x")
    dedicated = Memory(tmp_path / "d", shared=False)
    dedicated.remember("alice", True, "prefers W&B links")
    assert "W&B" in dedicated.recall("alice", True)


def test_task_visibility(tmp_path):
    st = ManagerState(tmp_path)
    st.track("t-1", "worker-a", "D1", "alice")
    st.track("t-2", "worker-a", "C1/111.1", "bob")
    t1, t2 = st.tasks["t-1"], st.tasks["t-2"]
    dm_alice = SimpleNamespace(private=True, requester="alice", conv=SimpleNamespace(key="D1"))
    thread = SimpleNamespace(private=False, requester="alice",
                             conv=SimpleNamespace(key="C1/111.1"))
    assert visible(dm_alice, t1) and not visible(dm_alice, t2)
    assert visible(thread, t2) and not visible(thread, t1)


def test_state_persists(tmp_path):
    st = ManagerState(tmp_path)
    st.conversation("C1", "111.1").session_id = "s-1"
    st.engaged.add("C1/111.1")
    st.track("t-1", "worker-a", "C1/111.1", "alice")
    again = ManagerState(tmp_path)
    assert again.conversations["C1/111.1"].session_id == "s-1"
    assert "C1/111.1" in again.engaged and "t-1" in again.tasks


# ── Slack routing ────────────────────────────────────────────────────────────

class FakeWeb:
    def __init__(self, replies=()):
        self.posts, self.replies = [], list(replies)

    async def chat_postMessage(self, **kw):
        self.posts.append(kw)

    async def conversations_replies(self, **kw):
        return {"messages": self.replies}


def make_io(may_use=lambda h: (True, ""), replies=()):
    seen = []

    async def on_message(msg):
        seen.append(msg)

    io = SlackIO.__new__(SlackIO)
    io.web, io.members, io.may_use, io.on_message = FakeWeb(replies), \
        {"U-ALICE": "alice", "U-BOB": "bob"}, may_use, on_message
    io.engaged, io.me, io.inbox, io.bot_token = set(), "U-BOT", "/tmp/unused", "x"
    return io, seen


def test_dm_and_mentions():
    io, seen = make_io()
    asyncio.run(io._on_event({"type": "message", "channel_type": "im", "channel": "D1",
                              "user": "U-ALICE", "ts": "1.0", "text": "hi"}))
    asyncio.run(io._on_event({"type": "app_mention", "channel": "C1", "user": "U-BOB",
                              "ts": "2.0", "text": "<@U-BOT> run exp 42"}))
    assert [(m.private, m.mentioned, m.human, m.text) for m in seen] == [
        (True, True, "alice", "hi"), (False, True, "bob", "run exp 42")]
    assert "C1/2.0" in io.engaged and seen[1].thread_ts == "2.0"


def test_threads_only_when_involved():
    io, seen = make_io()
    other = {"type": "message", "channel_type": "channel", "channel": "C1", "user": "U-ALICE",
             "ts": "5.0", "thread_ts": "4.0", "text": "unrelated"}
    asyncio.run(io._on_event(other))
    assert seen == []                                      # not involved: never sees it
    io.engaged.add("C1/4.0")
    asyncio.run(io._on_event(other))
    assert seen and not seen[0].mentioned                  # involved: sees it, may stay silent


def test_history_read_on_first_mention_midthread():
    io, seen = make_io(replies=[
        {"ts": "7.0", "user": "U-ALICE", "text": "loss diverges after step 3k"},
        {"ts": "7.5", "user": "U-BOB", "text": "lr too high?"},
        {"ts": "8.0", "user": "U-ALICE", "text": "<@U-BOT> can you check"}])
    asyncio.run(io._on_event({"type": "app_mention", "channel": "C1", "user": "U-ALICE",
                              "ts": "8.0", "thread_ts": "7.0", "text": "<@U-BOT> can you check"}))
    assert seen[0].history == ["alice: loss diverges after step 3k", "bob: lr too high?"]


def test_strangers_and_dedicated():
    io, seen = make_io(may_use=lambda h: (h == "alice", "I'm dedicated to alice."))
    asyncio.run(io._on_event({"type": "message", "channel_type": "im", "channel": "D2",
                              "user": "U-STRANGER", "ts": "1.0", "text": "hi"}))
    asyncio.run(io._on_event({"type": "message", "channel_type": "im", "channel": "D3",
                              "user": "U-BOB", "ts": "1.0", "text": "hi"}))
    assert seen == []
    assert [p["text"] for p in io.web.posts] == [
        "Sorry, I only work with members of this lab.", "I'm dedicated to alice."]


def test_bots_and_silence_ignored():
    io, seen = make_io()
    asyncio.run(io._on_event({"type": "message", "channel_type": "im", "channel": "D1",
                              "user": "U-ALICE", "bot_id": "B1", "ts": "1.0", "text": "x"}))
    assert seen == []
    asyncio.run(io.post("C1", "1.0", "<silent>"))
    assert io.web.posts == []


def test_compose():
    lab = registry.load(EXAMPLE)
    msg = SimpleNamespace(human="alice", text="train it", history=["bob: data is ready"],
                          files=["/w/inbox/x/data.csv"], mentioned=False)
    p = compose(lab, msg)
    assert "Earlier in this thread" in p and "[Alice (alice)]: train it" in p
    assert "data.csv" in p and "<silent>" in p


# ── follow-ups ───────────────────────────────────────────────────────────────

class FakeWorkers:
    def __init__(self, views):
        self.views = views

    async def call(self, worker, tool, args=None):
        return self.views.pop(0)


def test_followup_posts_progress_approval_and_finish(tmp_path):
    st = ManagerState(tmp_path)
    conv = st.conversation("C1", "1.0")
    st.track("t-1", "worker-a", conv.key, "alice")
    views = [
        {"state": "running", "progress": [{"message": "env ready"}]},
        {"state": "awaiting_approval", "progress": [{"message": "env ready"}]},
        {"state": "done", "progress": [{"message": "env ready"}, {"message": "trained"}],
         "result": {"summary": "loss 0.1", "artifacts": ["plot.png"]}},
    ]
    posts, finished = [], []

    async def post(c, text):
        posts.append(text)

    async def done(c, t, v):
        finished.append(followup.finished_prompt(t, v))

    for _ in range(3):
        asyncio.run(followup.check_once(st, FakeWorkers(views), post, done,
                                        mention=lambda h: f"<@{h}>"))
    assert posts[0].endswith("env ready") and "approval from <@alice>" in posts[1]
    assert posts[2].endswith("trained") and len(posts) == 3
    assert "loss 0.1" in finished[0] and "plot.png" in finished[0]
    assert st.open_tasks() == []


# ── manifests ────────────────────────────────────────────────────────────────

def test_manifests():
    lab = registry.load(EXAMPLE)
    m = slack_manifest.for_agent(lab, "mgr-alice")
    assert {"files:read", "files:write", "app_mentions:read"} <= set(m["oauth_config"]["scopes"]["bot"])
    assert m["settings"]["socket_mode_enabled"] and not m["settings"]["interactivity"]["is_enabled"]
    a = slack_manifest.approvals(lab)
    assert a["oauth_config"]["scopes"]["bot"] == ["chat:write", "im:write", "im:history"]
    assert a["settings"]["interactivity"]["is_enabled"]
    with pytest.raises(ValueError, match="only managers and the secretary"):
        slack_manifest.for_agent(lab, "worker-a")


def test_secretary_expiry_notices():
    import datetime as dt
    from agentic_lab.secretary.brain import expiry_notices
    lab = registry.load(EXAMPLE)
    soon = expiry_notices(lab, today=dt.date(2026, 12, 1))
    assert [(c["name"], d) for c, d in soon] == [("github/bot-pat", 10)]
    assert expiry_notices(lab, today=dt.date(2026, 10, 17)) == []


def test_idle_watch():
    import datetime as dt
    from agentic_lab.secretary.power import IdleWatch, powerable
    lab = registry.load(EXAMPLE)
    w = IdleWatch()
    t0 = dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc)
    snap = lambda q: {"agents": {"worker-a": {"up": True, "queue": q},
                                 "worker-b": {"up": True, "queue": {}}}}
    assert w.candidates(lab, snap({"done": 3}), now=t0) == []
    later = t0 + dt.timedelta(minutes=31)
    assert set(w.candidates(lab, snap({"done": 3}), now=later)) == {"gpu-a", "gpu-b"}
    w2 = IdleWatch()
    w2.candidates(lab, snap({"running": 1}), now=t0)
    assert "gpu-a" not in w2.candidates(lab, snap({"running": 1}), now=later)
    ok, why = powerable(lab, "nuc-01")
    assert not ok and "Nebius VM" in why
    assert powerable(lab, "gpu-a")[0]


def test_admin_only_actions(monkeypatch):
    """Powering off a busy machine and asking for someone else's token: admins only,
    enforced in code. A busy machine needs force_off even from an admin."""
    from agentic_lab.secretary import power as P
    from agentic_lab.secretary.service import Secretary
    lab = registry.load(EXAMPLE)
    lab.humans["alice"]["admin"] = True
    lab.humans["bob"] = {"name": "Bob", "slack_user": "U-BOB"}
    stopped = []

    async def stop(lab_, host):
        stopped.append(host)
    monkeypatch.setattr(P, "stop", stop)
    sec = Secretary.__new__(Secretary)
    sec.lab, sec.slack, sec.events, sec.workers = lab, None, None, None
    sec.state = SimpleNamespace(save=None)
    q = {"running": 1}
    sec.latest = {"state": {"agents": {"worker-a": {"up": True, "queue": q},
                                       "worker-b": {"up": True, "queue": {}}}}}

    def run(coro):
        return asyncio.run(coro)
    assert P.busy(lab, "gpu-a", sec.latest["state"]) == "worker-a has 1 running"
    assert P.busy(lab, "gpu-b", sec.latest["state"]) is None
    assert run(sec.ctx("bob").power("gpu-b", "off")).startswith("gpu-b: powering off")
    assert "Only an admin (Alice)" in run(sec.ctx("bob").power("gpu-a", "off"))
    assert "refused" in run(sec.ctx("bob").power("gpu-a", "force_off"))
    assert "use force_off" in run(sec.ctx("alice").power("gpu-a", "off"))
    assert run(sec.ctx("alice").power("gpu-a", "force_off")).startswith("gpu-a: powering off")
    assert stopped == ["gpu-b", "gpu-a"]
    assert "refused" in run(sec.ctx("bob").request_token("alice"))


def test_secretary_home_tab():
    import datetime as dt
    from agentic_lab.secretary import home
    now = dt.datetime.now(dt.timezone.utc)
    state = {"lab": "dev-lab", "at": now.isoformat(),
             "agents": {"worker-a": {"name": "worker-a", "role": "worker", "up": True,
                                     "queue": {"running": 1, "queued": 2},
                                     "recent": [{"id": "t-1", "state": "running", "requester": "bob",
                                                 "instruction": "train", "created": now.isoformat()},
                                                {"id": "t-2", "state": "done", "requester": "alice",
                                                 "instruction": "eval", "created": now.isoformat()}]}},
             "accounts": {"alice": {"name": "Alice", "agents": ["worker-a"], "limits": {
                 "seven_day": {"status": "allowed", "utilization": 0.34, "observed_at": now.isoformat(),
                               "resets_at": (now + dt.timedelta(days=3)).isoformat(), "from": "worker-a"}},
                 "tokens_today": {"worker-a": {"new": 1000, "cached": 9000}}}},
             "approvals": {"pending": [{"agent": "worker-a", "human": "alice", "detail": "curl x",
                                        "expires": now.isoformat()}]},
             "hosts": {"gpu-a": {"state": "RUNNING"}}}
    v = home.view(state, "alice", "http://sec:8774/", admin=True)
    text = str(v)
    assert v["type"] == "home" and "▰▰▰▱▱▱▱▱▱▱" in text and "*34%*" in text
    assert "1 waiting for you" in text and "busy" in text and "gpu-a" in text
    assert text.index("eval") < text.index("train")          # my tasks first
    assert "http://sec:8774/" in text and "each agent's log" in text
    lab = registry.load(EXAMPLE)
    m = slack_manifest.for_agent(lab, "secretary")
    assert m["features"]["app_home"]["home_tab_enabled"] and "app_home_opened" in m["settings"]["event_subscriptions"]["bot_events"]
    assert not slack_manifest.for_agent(lab, "mgr-alice")["features"]["app_home"]["home_tab_enabled"]


def test_home_opened_routes_to_callback():
    io, seen = make_io()
    opened = []

    async def on_home(user):
        opened.append(user)
    io.on_home = on_home
    asyncio.run(io._on_event({"type": "app_home_opened", "user": "U-ALICE", "tab": "home"}))
    asyncio.run(io._on_event({"type": "app_home_opened", "user": "U-STRANGER", "tab": "home"}))
    asyncio.run(io._on_event({"type": "app_home_opened", "user": "U-ALICE", "tab": "messages"}))
    assert opened == ["U-ALICE"] and seen == []
