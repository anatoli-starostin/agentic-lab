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
    dedicated.remember("alice", True, "prefers short summaries")
    assert "short summaries" in dedicated.recall("alice", True)


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

    async def conversations_replies(self, limit=1000, cursor=None, **kw):
        start = int(cursor or 0)                    # pages, oldest first, like Slack
        page = self.replies[start:start + limit]
        more = start + limit < len(self.replies)
        return {"messages": page,
                "response_metadata": {"next_cursor": str(start + limit) if more else ""}}


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


def test_history_keeps_the_threads_start_and_its_last_50():
    """A long thread: the first 5 messages (they set the topic) and the last 50 before the
    mention, read across pages; what's in between is marked as skipped."""
    replies = [{"ts": f"{100 + i}.0", "user": "U-BOB", "text": f"m{i}"} for i in range(450)]
    mention = {"ts": "600.0", "user": "U-ALICE", "text": "<@U-BOT> summarise"}
    io, seen = make_io(replies=replies + [mention])
    asyncio.run(io._on_event({"type": "app_mention", "channel": "C1", "user": "U-ALICE",
                              "ts": "600.0", "thread_ts": "100.0", "text": mention["text"]}))
    h = seen[0].history
    assert h[:5] == [f"bob: m{i}" for i in range(5)]
    assert h[5] == "[... 395 messages skipped ...]"
    assert h[6:] == [f"bob: m{i}" for i in range(400, 450)] and len(h) == 56


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
    # the GPU counts too: a process on it (anyone's), or utilization
    gpu_proc = {"up": True, "queue": {}, "gpu_processes": [{"pid": 7, "name": "python"}]}
    hot = {"up": True, "queue": {}, "gpus": [{"name": "H100", "util": 85}]}
    cool = {"up": True, "queue": {}, "gpus": [{"name": "H100", "util": 0}], "gpu_processes": []}
    warm = {"up": True, "queue": {}, "gpus": [{"name": "H100", "util": 1}], "gpu_processes": []}
    assert P.worker_busy("w", warm) == "GPU 1% busy"          # strict: anything above 0%
    for seen, why in ((gpu_proc, "1 process(es) on the GPU (python)"), (hot, "GPU 85% busy"),
                      (cool, None)):
        assert P.busy(lab, "gpu-b", {"agents": {"worker-b": seen}}) == why
    w = P.IdleWatch()
    import datetime as dt
    t0 = dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc)
    snap = {"agents": {"worker-a": cool, "worker-b": gpu_proc}}
    w.candidates(lab, snap, now=t0)
    later = w.candidates(lab, snap, now=t0 + dt.timedelta(hours=2))
    assert "gpu-b" not in later and "gpu-a" in later       # never idle-stopped mid-GPU-job


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


# ── staying reachable: turn budget, busy note ────────────────────────────────

class _FakeClient:
    """Stands in for ClaudeSDKClient: the first request hangs (long work); later ones answer."""

    def __init__(self, options):
        self.options = options
        self.queries, self.interrupted = [], 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, prompt):
        self.queries.append(prompt)

    async def interrupt(self):
        self.interrupted += 1

    async def receive_response(self):
        from claude_agent_sdk import AssistantMessage, TextBlock
        if len(self.queries) == 1 and not self.interrupted:
            await asyncio.sleep(3600)               # the long work
        yield AssistantMessage(content=[TextBlock(text=f"reply to: {self.queries[-1][:20]}")],
                               model="m")


def test_a_long_turn_is_cut_off_and_wrapped_up():
    from agentic_lab.manager import brain as B
    clients = []

    def factory(options):
        clients.append(_FakeClient(options))
        return clients[-1]
    br = B.Brain("Flash", "lab", "/tmp", lambda grants: {}, turn_budget_s=0.2)
    br.client_factory = factory
    conv = SimpleNamespace(key="c", session_id=None)
    ctx = SimpleNamespace(conv=conv, requester="alice", state=SimpleNamespace(save=lambda: None))
    reply = asyncio.run(br.turn(ctx, "refactor the whole repo"))
    c = clients[0]
    assert c.interrupted >= 1 and c.queries[1] == B.TIME_UP
    assert reply == "reply to: " + B.TIME_UP[:20]
    assert "5 minutes" in B.system_prompt("Flash", "lab")
    assert "1 minutes" not in B.system_prompt("Flash", "lab", 2)


def test_busy_note_once_per_busy_spell(monkeypatch):
    from agentic_lab.manager import service as S
    posted = []

    class Slack:
        async def react(self, *a, **k):
            pass

        async def post(self, channel, thread, text):
            posted.append(text)
    m = S.Manager.__new__(S.Manager)
    m._locks, m._told_busy, m.slack = {}, set(), Slack()
    conv = SimpleNamespace(key="D1", channel="D1", thread_ts=None)
    m.state = SimpleNamespace(conversation=lambda ch, ts: conv)
    m.lab = None
    started = asyncio.Event()

    async def turn(conv_, requester, private, prompt):
        lock = m._locks.setdefault(conv_.key, asyncio.Lock())
        async with lock:
            m._told_busy.discard(conv_.key)
            started.set()
            await asyncio.sleep(0.05)
            return "done"
    m._turn = turn
    monkeypatch.setattr(S, "compose", lambda lab, msg: msg.text)
    msg = lambda: SimpleNamespace(channel="D1", thread_ts=None, ts="1", human="alice",  # noqa
                                  private=True, mentioned=True, text="hi", files=[],
                                  history=[], user="U1")

    async def go():
        first = asyncio.create_task(m.on_message(msg()))
        await started.wait()
        await asyncio.gather(m.on_message(msg()), m.on_message(msg()), first)
    asyncio.run(go())
    assert posted.count(S.BUSY) == 1 and posted.count("done") == 3


# ── guests: other members talking to someone's dedicated manager ─────────────

def test_guest_mode(monkeypatch):
    from agentic_lab.manager import brain as B
    from agentic_lab.manager import service as S
    lab = registry.load(EXAMPLE)
    lab.humans["bob"] = {"name": "Bob", "slack_user": "U-BOB"}
    m = S.Manager.__new__(S.Manager)
    m.lab, m.agent_id = lab, "mgr-alice"                  # dedicated to alice
    assert m.may_use("bob") == (True, "")                 # anyone in the lab may talk...
    assert m.is_guest("bob") and not m.is_guest("alice")  # ...but bob is a guest

    clients = []

    def factory(options):
        clients.append(_FakeClient(options))
        return clients[-1]
    br = B.Brain("Flash", "lab", "/tmp", lambda grants: {}, turn_budget_s=300,
                 env={"GH_TOKEN": "ghp_x", "CLAUDE_CODE_OAUTH_TOKEN": "seat"},
                 prompt_extra="GitHub: you act as flash-bot")
    br.client_factory = factory
    monkeypatch.setattr(B, "GUEST_TURN_BUDGET_S", 0.2)
    conv = SimpleNamespace(key="c", session_id=None)

    def ctx(guest):
        return SimpleNamespace(conv=conv, requester="bob" if guest else "alice", guest=guest,
                               owner_name="Alice", guest_name="Bob",
                               state=SimpleNamespace(save=lambda: None))
    asyncio.run(br.turn(ctx(True), "run my training job"))
    o = clients[-1].options
    assert o.mcp_servers == {} and o.allowed_tools == []
    assert {"Bash", "Read", "WebFetch"} <= set(o.disallowed_tools)
    assert "GH_TOKEN" not in o.env and o.env["CLAUDE_CODE_OAUTH_TOKEN"] == "seat"
    prompt = o.system_prompt["append"]
    assert "Bob" in prompt and "guest" in prompt and "flash-bot" not in prompt
    assert clients[-1].queries[1] == B.GUEST_TIME_UP          # 0.2 s guest limit hit
    allowed = asyncio.run(o.can_use_tool("Bash", {"command": "ls"}, None))
    assert allowed.behavior == "deny"

    br.turn_budget_s = 0.3
    clients.clear()
    asyncio.run(br.turn(ctx(False), "hi"))                    # the owner: everything
    o = clients[-1].options
    assert "lab" in o.mcp_servers and o.env["GH_TOKEN"] == "ghp_x"
    assert "flash-bot" in o.system_prompt["append"]


def test_gpu_idle_only_after_several_zero_readings():
    from agentic_lab.secretary import power as P
    P._gpu_recent.clear()

    def snap(util):
        return {"agents": {"worker-b": {"role": "worker", "up": True, "queue": {},
                                        "gpus": [{"name": "H100", "util": util}]}}}
    s = snap(40)
    P.note_gpu(s)
    assert P.worker_busy("w", s["agents"]["worker-b"]) == "GPU 40% busy"
    for i in range(P.GPU_IDLE_READINGS - 1):                   # 0% now, but not long enough
        s = snap(0)
        P.note_gpu(s)
        assert P.worker_busy("w", s["agents"]["worker-b"]).startswith("GPU was 40% busy")
    s = snap(0)
    P.note_gpu(s)                                              # 0% in 5 readings in a row
    assert P.worker_busy("w", s["agents"]["worker-b"]) is None
    P._gpu_recent.clear()


def test_system_prompts_render():
    """Every agent's instructions are f-strings: literal braces must be doubled, or the
    agent fails on every conversation."""
    from agentic_lab.manager import brain as MB
    from agentic_lab.secretary import brain as SB
    assert "github.accounts" in SB.system_prompt("Iris", "lab")
    assert "{app_id, email}" in SB.system_prompt("Iris", "lab")
    assert "Flash" in MB.system_prompt("Flash", "lab")
    assert "Bob" in MB.guest_prompt("Alice", "Bob")


def test_idle_stop_with_managers():
    """A host is judged by its workers alone (tasks, GPU, keep-awake), managers on it or not;
    with idle_stop_with_managers: false a manager keeps it busy."""
    import datetime as dt
    from agentic_lab.secretary import power as P
    lab = registry.load(EXAMPLE)
    lab.agents["mgr-alice"]["host"] = "gpu-a"
    t0 = dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc)
    later = t0 + dt.timedelta(minutes=31)
    cool = {"up": True, "queue": {}, "gpus": [{"name": "H100", "util": 0}],
            "gpu_processes": [], "gpu_readings": P.GPU_IDLE_READINGS}
    idle = {"agents": {"worker-a": cool, "mgr-alice": {"up": True}}}

    def stoppable(snap):
        w = P.IdleWatch()
        w.candidates(lab, snap, now=t0)
        return "gpu-a" in w.candidates(lab, snap, now=later)
    # (a) opted out: busy, never idle-stopped
    lab.hosts["gpu-a"]["idle_stop_with_managers"] = False
    assert "(manager)" in P.busy(lab, "gpu-a", idle) and "idle_stop_with_managers: false" in \
        P.busy(lab, "gpu-a", idle)
    assert not stoppable(idle)
    # (b) by default: idle worker -> eligible
    del lab.hosts["gpu-a"]["idle_stop_with_managers"]
    assert P.busy(lab, "gpu-a", idle) is None
    assert stoppable(idle)
    # ...but a busy worker, a warm GPU or a keep-awake file still block it
    for extra, why in (({"queue": {"queued": 1}}, "worker-a has 1 queued"),
                       ({"gpus": [{"name": "H100", "util": 3}]}, "GPU 3% busy"),
                       ({"keep_awake": [{"user": "alice", "note": ""}]}, "kept awake by alice")):
        snap = {"agents": {"worker-a": {**cool, **extra}, "mgr-alice": {"up": True}}}
        assert P.busy(lab, "gpu-a", snap).startswith(why)
        assert not stoppable(snap)
    # a host with only a manager has nothing to measure: never idle-stopped
    lab.agents["worker-a"]["host"] = "gpu-b"
    assert not stoppable(idle)


def test_idle_stop_with_managers_check():
    import yaml
    d = yaml.safe_load(EXAMPLE.read_text())
    d["hosts"]["mgr-vm-01"]["idle_stop_with_managers"] = True     # the default anyway
    d["hosts"]["gpu-a"]["idle_stop_with_managers"] = False
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "lab.yaml"
        p.write_text(yaml.safe_dump(d))
        warns = [str(i) for i in registry.check(registry.load(p)) if i.level == "warning"]
    assert any("mgr-vm-01" in w and "idle_stop_with_managers" in w for w in warns)
    assert not any("gpu-a" in w and "idle_stop_with_managers" in w for w in warns)
