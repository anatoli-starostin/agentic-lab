"""Grants widen the sandbox for as long as they last, and only where a grant is meant to."""
import asyncio
import datetime as dt
from pathlib import Path

import pytest
import yaml

from agentic_lab import registry
from agentic_lab.policy import grants as G
from agentic_lab.policy.sandbox import settings
from agentic_lab.worker import runner as RN
from agentic_lab.worker import tasks as T
from agentic_lab.worker.runner import Decision, SdkRunner

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"
UTC = dt.timezone.utc


def _iso(t):
    return t.isoformat(timespec="seconds")


@pytest.mark.parametrize("tool", ["Bash", "mcp__lab__start_run", "mcp__lab__*",
                                  "mcp__lab__stop_*"])
def test_some_tools_are_not_grantable(tool, tmp_path):
    with pytest.raises(G.GrantError):
        G.validate("tool", tool)
    d = yaml.safe_load(EXAMPLE.read_text())
    a = next(iter(d["agents"]))
    d["agents"][a]["grants"] = [{"tool": tool}]
    p = tmp_path / "lab.yaml"
    p.write_text(yaml.safe_dump(d))
    errs = [i for i in registry.check(registry.load(p)) if i.level == "error"]
    assert any("grants[0]" in str(i) for i in errs)


def test_other_tools_still_are():
    G.validate("tool", "WebFetch")
    G.validate("tool", "mcp__papers__*")


def test_settings_never_pre_allow_reserved_tools(tmp_path):
    gs = [G.Grant("tool", "mcp__lab__*"), G.Grant("tool", "Bash"), G.Grant("tool", "WebFetch")]
    allow = settings(str(tmp_path), grant_list=gs)["permissions"]["allow"]
    assert "WebFetch" in allow
    assert not any(x == "Bash" or x.startswith("mcp__lab__") for x in allow)


class Asks:
    def __init__(self):
        self.asked = []

    async def ask(self, task, tool, inp, violations, stop=None, filed=None):
        self.asked.append(tool)
        return Decision("deny", message="no")

    async def active_grants(self):
        return []


def _setup(tmp_path):
    store = T.TaskStore(tmp_path / "tasks")
    t = store.update(store.submit("x", "alice", "m").id, state=T.RUNNING)
    ap = Asks()
    return store, t, ap, SdkRunner("w", str(tmp_path), lambda g: {}, approver=ap)


def test_leaving_the_sandbox_always_asks(tmp_path):
    store, t, ap, r = _setup(tmp_path)
    later = _iso(dt.datetime.now(UTC) + dt.timedelta(hours=1))
    grants = [{"kind": "tool", "value": "Bash", "expires": later}]
    opts = r._options(t, store, {}, {"violations": [], "restart_for": None}, grants)
    asyncio.run(opts.can_use_tool("Bash", {"command": "make",
                                           "dangerouslyDisableSandbox": True}, None))
    assert ap.asked == ["Bash"]


def test_a_tool_grant_ends_at_its_expiry(tmp_path):
    store, t, ap, r = _setup(tmp_path)
    now = dt.datetime.now(UTC)
    grants = [{"kind": "tool", "value": "WebFetch", "expires": _iso(now + dt.timedelta(hours=1))},
              {"kind": "tool", "value": "WebSearch", "expires": _iso(now - dt.timedelta(minutes=1))}]
    opts = r._options(t, store, {}, {"violations": [], "restart_for": None}, grants)
    asyncio.run(opts.can_use_tool("WebFetch", {"url": "https://a.org"}, None))
    asyncio.run(opts.can_use_tool("WebSearch", {"query": "q"}, None))
    assert ap.asked == ["WebSearch"]


def test_the_session_restarts_without_an_expired_grant(tmp_path, monkeypatch):
    store, t, ap, r = _setup(tmp_path)
    soon = _iso(dt.datetime.now(UTC) + dt.timedelta(minutes=60))
    given = [[{"kind": "domain", "value": "a.example.org", "expires": soon}], []]
    seen = []

    async def active():
        return given.pop(0)
    ap.active_grants = active

    async def session(task, store_, outcome, sess, grants, prompt):
        seen.append((grants, sess["narrow_at"]))
        if len(seen) == 1:
            sess["expired"] = True          # what the watcher does at narrow_at
            return None, None
        outcome.update(result={"summary": "ok", "artifacts": []}, success=True)
        return None, None
    monkeypatch.setattr(r, "_session", session)
    asyncio.run(r.run(t, store))
    assert [g for g, _ in seen] == [[{"kind": "domain", "value": "a.example.org",
                                      "expires": soon}], []]
    assert seen[0][1] == dt.datetime.fromisoformat(soon) and seen[1][1] is None
    assert store.get(t.id).state == T.DONE


def test_the_watcher_interrupts_at_expiry(tmp_path, monkeypatch):
    store, t, ap, r = _setup(tmp_path)
    monkeypatch.setattr(RN, "CANCEL_POLL_S", 0.01)
    interrupted = []

    class Client:
        async def interrupt(self):
            interrupted.append(True)
    sess = {"narrow_at": dt.datetime.now(UTC) + dt.timedelta(milliseconds=50), "expired": False}
    asyncio.run(asyncio.wait_for(r._watch_cancel(t, store, Client(), sess), 2))
    assert interrupted == [True] and sess["expired"]
