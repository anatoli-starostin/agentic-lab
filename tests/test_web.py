"""The web UI renders a snapshot, escaping everything."""
import datetime as dt

from agentic_lab.secretary import web

NOW = dt.datetime.now(dt.timezone.utc)
STATE = {
    "lab": "dev-lab", "at": NOW.isoformat(),
    "agents": {
        "worker-a": {"name": "worker-a", "role": "worker", "human": "alice", "up": True,
                     "gpu": "H100", "queue": {"running": 1}, "disk_free_gb": 100,
                     "recent": [{"id": "t-1", "state": "running", "requester": "alice",
                                 "manager": "mgr", "created": NOW.isoformat(), "finished": None,
                                 "instruction": "<script>alert(1)</script> train"}]},
        "mgr": {"name": "Ada", "role": "manager", "human": "alice", "service": "active"},
    },
    "accounts": {"alice": {"name": "Alice", "agents": ["worker-a", "mgr"],
                           "limits": {"five_hour": {"utilization": 0.62, "status": "allowed",
                                                    "resets_at": "2026-10-17T16:40:00+00:00",
                                                    "observed_at": (NOW - dt.timedelta(minutes=3)).isoformat(),
                                                    "from": "worker-a"}},
                           "tokens_today": {"worker-a": {"new": 12345, "cached": 800000}}}},
    "approvals": {"pending": [], "grants": {"worker-a": [{"kind": "domain", "value": "arxiv.org",
                                                          "granted_by": "alice",
                                                          "expires": "2026-10-17T16:00:00+00:00"}]}},
}


def test_render():
    page = web.render(STATE)
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "62% used" in page and "3 min ago" in page and "from worker-a" in page
    assert "Current session" in page and "resets in" in page
    assert "arxiv.org" in page and "812k on this account" in page and "busy" in page
    assert "var(--c1)" in page and "12k new" in page
    assert "aria-valuenow='62'" in page
    assert web.render(None).startswith("<!doctype html>")


def test_ago():
    assert web.ago((NOW - dt.timedelta(hours=3)).isoformat(), NOW) == "3 h ago"
    assert web.ago(None) == "never"


def test_log_access():
    """#44: admins see every log; a member their dedicated agents' whole log, their own
    entries of shared agents and the secretary; nothing of others' dedicated agents."""
    from pathlib import Path

    from agentic_lab import registry
    lab = registry.load(Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml")
    lab.humans["alice"]["admin"] = False
    lab.humans["bob"] = {"name": "Bob"}
    lab.agents["mgr-alice"]["access"] = "dedicated"
    lab.agents["worker-a"]["access"] = "shared"
    lab.agents["worker-b"].update(access="dedicated", human="alice")
    acc = lambda m, a: web.log_access(lab, m, a)          # noqa: E731
    assert acc("alice", "mgr-alice") == "all" and acc("alice", "worker-b") == "all"
    assert acc("bob", "mgr-alice") is None and acc("bob", "worker-b") is None
    assert acc("bob", "worker-a") == "own" and acc("bob", "secretary") == "own"
    assert acc(None, "worker-a") is None and acc("mallory", "worker-a") is None
    lab.humans["bob"]["admin"] = True
    assert acc("bob", "mgr-alice") == "all"
    evs = [{"member": "bob", "text": "hi"}, {"member": "carol", "text": "x"},
           {"task": "t-1", "text": "bob's task"}, {"task": "t-2", "text": "carol's"},
           {"kind": "note", "text": "no owner"}]
    assert [e["text"] for e in web.own_entries(evs, "bob", {"t-1"})] == ["hi", "bob's task"]


def test_logs_page_admin_only(free_port):
    import asyncio
    import threading
    import time

    import httpx2
    import uvicorn

    asked = []

    async def events(agent, hours, member=None):
        asked.append(member)
        return [{"at": NOW.isoformat(), "kind": "tool", "text": "Bash: <b>ls</b>", "task": "t-1"}]

    def run(scope):
        async def viewer(ip):
            return "alice"
        server = uvicorn.Server(uvicorn.Config(web.app(lambda: STATE, events, viewer,
                                                       access=lambda m, a: scope),
                                               host="127.0.0.1", port=free_port, log_level="error"))
        th = threading.Thread(target=server.run, daemon=True)
        th.start()
        while not server.started:
            time.sleep(0.02)
        try:
            return httpx2.get(f"http://127.0.0.1:{free_port}/logs/worker-a?hours=1")
        finally:
            server.should_exit = True
            th.join()

    assert run(None).status_code == 403
    r = run("all")
    assert r.status_code == 200 and "&lt;b&gt;ls&lt;/b&gt;" in r.text and "t-1" in r.text
    assert run("own").status_code == 200 and asked == [None, "alice"]   # filtered for alice


def test_event_log(tmp_path):
    from agentic_lab.eventlog import EventLog, tool_summary
    log = EventLog(tmp_path)
    log.add("tool", tool_summary("Bash", {"command": "nvidia-smi"}), task="t-1")
    log.add("reply", "x" * 5000)
    evs = log.read(since=dt.timedelta(hours=1))
    assert evs[0]["text"] == "Bash: nvidia-smi" and evs[0]["task"] == "t-1"
    assert "more characters" in evs[1]["text"] and len(evs[1]["text"]) < 2100


def test_meter_without_percentage():
    html = web._meter("five_hour", {"status": "allowed", "utilization": None,
                                    "resets_at": (NOW + dt.timedelta(hours=2, minutes=52)).isoformat(),
                                    "observed_at": NOW.isoformat(), "from": "mgr"})
    assert "Within limit" in html and "no percentage reported" in html and "meter" not in html.split("mstatus")[0].split("mhead")[0]
    assert "resets in 2 h 5" in html


def test_human_numbers():
    assert web.human(888114) == "888k" and web.human(1_234_567) == "1.2M" and web.human(42) == "42"
