"""The approval broker: decisions, timeouts, grants, its HTTP API, the Slack channel's
handling of clicks and replies, and the worker's approver against a real broker."""
import asyncio
import datetime as dt
import threading
import time

import httpx2
import pytest
import uvicorn

from agentic_lab.broker import core as C
from agentic_lab.broker.http import app
from agentic_lab.broker.slack import SlackChannel, decided_blocks, request_blocks
from agentic_lab.worker import tasks as T
from agentic_lab.worker.approver import BrokerApprover
from agentic_lab.worker.runner import sandbox_violations

HUMANS = {"alice": {"slack_user": "U-ALICE", "timeout_s": 120},
          "bob": {"slack_user": "U-BOB", "timeout_s": 1}}
TOK_A = "w" * 40
BASH_OUT = {"command": "curl -s https://arxiv.org/abs/1234 -o paper.html",
            "dangerouslyDisableSandbox": True}


@pytest.fixture
def broker(tmp_path):
    return C.Broker(tmp_path, HUMANS)


def submit(b, human="alice", tool="Bash", inp=BASH_OUT, violations=(), task="t-1"):
    return b.submit("worker-a", human, tool, inp, task_id=task, violations=violations)


# ── what can be granted ──────────────────────────────────────────────────────

def test_grant_options():
    o = C.grant_options("Bash", BASH_OUT, ["deny network-outbound files.example.org:443"])
    assert o == [{"kind": "domain", "value": "files.example.org"},
                 {"kind": "domain", "value": "arxiv.org"}]
    assert C.grant_options("Write", {"file_path": "/data/shared/x.txt"}, []) == \
        [{"kind": "write", "value": "/data/shared"}]
    assert C.grant_options("Write", {"file_path": "/x.txt"}, []) == []      # never "/"
    assert C.grant_options("WebFetch", {"url": "https://a.org"}, []) == \
        [{"kind": "tool", "value": "WebFetch"}]
    assert C.grant_options("Bash", {"command": "curl http://10.0.0.1/x"}, []) == []  # no IPs


def test_sandbox_violations_parsed():
    text = ("Exit code 56\n<sandbox_violations>\ndeny network-outbound example.com:443 "
            "(host is not on the allow list)\n</sandbox_violations>")
    assert sandbox_violations(text) == [
        "deny network-outbound example.com:443 (host is not on the allow list)"]
    assert sandbox_violations("all fine") == []


# ── decisions ────────────────────────────────────────────────────────────────

def test_only_the_asked_human_decides(broker):
    r = submit(broker)
    with pytest.raises(C.BrokerError, match="only the person asked"):
        broker.decide(r.id, "once", "U-BOB")
    with pytest.raises(C.BrokerError, match="only the person asked"):
        broker.decide(r.id, "once", None)
    d = broker.decide(r.id, "once", "U-ALICE")
    assert d.state == C.DECIDED and d.decision["action"] == "once" and d.decision["by"] == "alice"
    with pytest.raises(C.BrokerError, match="already decided"):
        broker.decide(r.id, "deny", "U-ALICE")


def test_unknown_human_and_action(broker):
    with pytest.raises(C.BrokerError, match="unknown human"):
        submit(broker, human="mallory")
    r = submit(broker)
    with pytest.raises(C.BrokerError, match="unknown action"):
        broker.decide(r.id, "always", "U-ALICE")


def test_grants(broker):
    r = submit(broker, violations=["deny network-outbound arxiv.org:443"])
    d = broker.decide(r.id, "hour", "U-ALICE")
    g = d.decision["grants"]
    assert [x["value"] for x in g] == ["arxiv.org"] and g[0]["granted_by"] == "alice"
    assert broker.grants("worker-a")[0]["value"] == "arxiv.org"
    assert broker.grants("worker-b") == []                  # grants are per agent
    r2 = submit(broker, task="t-2")
    broker.decide(r2.id, "task", "U-ALICE")
    assert {x["task_id"] for x in broker.grants("worker-a")} == {None, "t-2"}


def test_nothing_to_grant(broker):
    r = submit(broker, inp={"command": "make install", "dangerouslyDisableSandbox": True})
    with pytest.raises(C.BrokerError, match="nothing to grant"):
        broker.decide(r.id, "hour", "U-ALICE")


def test_timeout_denies(broker):
    r = submit(broker, human="bob")                       # bob's timeout is 1 s
    assert broker.expire() == []
    time.sleep(1.1)
    assert [x.id for x in broker.expire()] == [r.id]
    assert broker.get(r.id).decision["action"] == "timeout"
    with pytest.raises(C.BrokerError, match="already decided"):
        broker.decide(r.id, "once", "U-BOB")              # too late


def test_manager_context_cannot_decide(broker):
    r = submit(broker)
    broker.add_context(r.id, "needed for the eval in task t-1")
    assert broker.get(r.id).state == C.PENDING and broker.get(r.id).context


# ── the HTTP API ─────────────────────────────────────────────────────────────

def _serve(asgi, port):
    server = uvicorn.Server(uvicorn.Config(asgi, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.02)
    return server, th


def test_http_api(broker, free_port):
    server, th = _serve(app(broker, {TOK_A: "worker-a", "x" * 40: "worker-b"}), free_port)
    url = f"http://127.0.0.1:{free_port}"
    try:
        h = {"Authorization": f"Bearer {TOK_A}"}
        assert httpx2.post(f"{url}/v1/requests", json={}).status_code == 401
        r = httpx2.post(f"{url}/v1/requests", headers=h, json={
            "human": "alice", "tool": "Bash", "input": BASH_OUT, "task_id": "t-1"}).json()
        assert r["state"] == "pending" and r["options"]
        # another worker can't see it
        other = httpx2.get(f"{url}/v1/requests/{r['id']}",
                           headers={"Authorization": "Bearer " + "x" * 40})
        assert other.status_code == 404
        # long poll returns as soon as the human decides
        threading.Timer(0.6, broker.decide, (r["id"], "once", "U-ALICE")).start()
        t0 = time.time()
        got = httpx2.get(f"{url}/v1/requests/{r['id']}", params={"wait": 10}, headers=h,
                         timeout=20).json()
        assert got["decision"]["action"] == "once" and time.time() - t0 < 5
        assert httpx2.get(f"{url}/v1/grants", headers=h).json() == {"agent": "worker-a",
                                                                   "grants": []}
    finally:
        server.should_exit = True
        th.join()


def test_worker_approver_end_to_end(broker, free_port):
    server, th = _serve(app(broker, {TOK_A: "worker-a"}), free_port)
    url = f"http://127.0.0.1:{free_port}"
    task = T.Task(id="t-1", instruction="x", requester="alice", manager="mgr-alice")
    try:
        def human(action, note=None):
            for _ in range(100):
                pending = broker.pending("alice")
                if pending:
                    broker.decide(pending[0].id, action, "U-ALICE", note=note)
                    return
                time.sleep(0.05)

        async def ask(action, note=None):
            threading.Thread(target=human, args=(action, note)).start()
            return await BrokerApprover(url, TOK_A).ask(
                task, "Bash", BASH_OUT, ["deny network-outbound arxiv.org:443"])

        assert asyncio.run(ask("once")).kind == "allow"
        g = asyncio.run(ask("hour"))
        assert g.kind == "grant" and g.grants[0]["value"] == "arxiv.org"
        d = asyncio.run(ask("deny", "use the local copy in /data"))
        assert d.kind == "deny" and "use the local copy in /data" in d.message
        assert asyncio.run(BrokerApprover(url, TOK_A).active_grants())[0]["value"] == "arxiv.org"
        bad = asyncio.run(BrokerApprover(url, "nope" * 10).ask(task, "Bash", BASH_OUT, []))
        assert bad.kind == "deny" and "can't be reached" in bad.message
    finally:
        server.should_exit = True
        th.join()


# ── the Slack channel ────────────────────────────────────────────────────────

class FakeWeb:
    def __init__(self):
        self.calls = []

    async def conversations_open(self, users):
        self.calls.append(("open", users))
        return {"channel": {"id": "D-ALICE"}}

    async def chat_postMessage(self, **kw):
        self.calls.append(("post", kw))
        return {"ts": "111.222"}

    async def chat_update(self, **kw):
        self.calls.append(("update", kw))

    async def chat_postEphemeral(self, **kw):
        self.calls.append(("ephemeral", kw))


class FakeSocketRequest:
    def __init__(self, type_, payload):
        self.type, self.payload, self.envelope_id = type_, payload, "env-1"


class FakeSocketClient:
    async def send_socket_mode_response(self, resp):
        pass


def _channel(broker):
    ch = SlackChannel.__new__(SlackChannel)
    ch.broker, ch.web, ch.loop = broker, FakeWeb(), None
    broker.listeners.append(ch._on_change)
    return ch


def test_slack_dm_buttons_and_decision(broker):
    ch = _channel(broker)

    async def scenario():
        ch.loop = asyncio.get_running_loop()
        r = submit(broker, violations=["deny network-outbound arxiv.org:443"])
        await asyncio.sleep(0.05)
        kind, post = ch.web.calls[1]
        assert ch.web.calls[0] == ("open", "U-ALICE") and kind == "post"
        labels = [e["text"]["text"] for b in post["blocks"] if b["type"] == "actions"
                  for e in b["elements"]]
        assert labels == ["Run once", "Allow arxiv.org · 1 hour", "Allow arxiv.org · this task",
                          "Deny"]
        assert broker.get(r.id).channel_ref == {"channel": "D-ALICE", "ts": "111.222"}
        # someone else clicks: refused
        await ch._on_socket(FakeSocketClient(), FakeSocketRequest("interactive", {
            "type": "block_actions", "user": {"id": "U-BOB"}, "channel": {"id": "D-ALICE"},
            "actions": [{"value": f"{r.id}|once"}]}))
        await asyncio.sleep(0.05)
        assert broker.get(r.id).state == C.PENDING
        # alice clicks "1 hour"
        await ch._on_socket(FakeSocketClient(), FakeSocketRequest("interactive", {
            "type": "block_actions", "user": {"id": "U-ALICE"}, "channel": {"id": "D-ALICE"},
            "actions": [{"value": f"{r.id}|hour"}]}))
        await asyncio.sleep(0.05)
        assert broker.get(r.id).decision["action"] == "hour"
        update = [c for c in ch.web.calls if c[0] == "update"][-1][1]
        assert "granted for 1 hour" in str(update["blocks"]) and "actions" not in str(update)

    asyncio.run(scenario())


def test_slack_thread_reply_denies_with_note(broker):
    ch = _channel(broker)

    async def scenario():
        ch.loop = asyncio.get_running_loop()
        r = submit(broker)
        await asyncio.sleep(0.05)
        await ch._on_socket(FakeSocketClient(), FakeSocketRequest("events_api", {"event": {
            "type": "message", "channel_type": "im", "channel": "D-ALICE",
            "thread_ts": "111.222", "user": "U-ALICE", "text": "use the cached copy"}}))
        await asyncio.sleep(0.05)
        d = broker.get(r.id).decision
        assert d["action"] == "deny" and d["note"] == "use the cached copy"

    asyncio.run(scenario())


def test_blocks_render():
    r = C.Request(id="r-1", agent="worker-a", task_id="t-1", human="alice", tool="Bash",
                  detail="make install", violations=[], options=[], created="x",
                  expires=dt.datetime(2026, 10, 17, 15, 0, tzinfo=dt.timezone.utc).isoformat())
    labels = [e["text"]["text"] for b in request_blocks(r) if b["type"] == "actions"
              for e in b["elements"]]
    assert labels == ["Run once", "Deny"]                   # nothing to grant: no grant buttons
    r.state, r.decision = C.DECIDED, {"action": "timeout", "by": None, "note": None}
    assert "timed out" in str(decided_blocks(r))


def test_broker_accepts_all_agent_tokens():
    from pathlib import Path
    from agentic_lab import registry
    from agentic_lab.broker.service import tokens_from
    lab = registry.load(Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml")
    t = tokens_from(lab, "worker-a: " + "a" * 40 + "\nmgr-alice: " + "b" * 40 +
                         "\nsecretary: " + "c" * 40 + "\n")
    assert set(t.values()) == {"worker-a", "mgr-alice", "secretary"}
