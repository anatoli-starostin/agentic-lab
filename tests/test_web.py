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
                           "tokens_today": {"worker-a": 12345}}},
    "approvals": {"pending": [], "grants": {"worker-a": [{"kind": "domain", "value": "arxiv.org",
                                                          "granted_by": "alice",
                                                          "expires": "2026-10-17T16:00:00+00:00"}]}},
}


def test_render():
    page = web.render(STATE)
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "62%" in page and "3 min ago" in page and "from worker-a" in page
    assert "arxiv.org" in page and "12,345" in page and "busy" in page
    assert web.render(None).startswith("<!doctype html>")


def test_ago():
    assert web.ago((NOW - dt.timedelta(hours=3)).isoformat(), NOW) == "3 h ago"
    assert web.ago(None) == "never"
