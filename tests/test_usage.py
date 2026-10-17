"""Seat usage: the probe's header parsing, when it probes, keeping a measured percentage,
the per-account merge and the web UI's "check now"."""
import asyncio
import datetime as dt

from starlette.testclient import TestClient

from agentic_lab.secretary import web
from agentic_lab.worker import usage as U

NOW = dt.datetime(2026, 10, 17, 12, 0, tzinfo=dt.timezone.utc)
RESET = int((NOW + dt.timedelta(hours=3)).timestamp())
HEADERS = {"anthropic-ratelimit-unified-5h-utilization": "0.12",
           "anthropic-ratelimit-unified-5h-reset": str(RESET),
           "anthropic-ratelimit-unified-5h-status": "allowed",
           "anthropic-ratelimit-unified-7d-utilization": "0.07",
           "anthropic-ratelimit-unified-7d-reset": str(RESET + 86400),
           "Anthropic-Ratelimit-Unified-7d-Status": "allowed"}


def test_parse_headers():
    r = U.parse_headers(HEADERS)
    assert r["five_hour"]["utilization"] == 0.12 and r["five_hour"]["status"] == "allowed"
    assert r["seven_day"]["utilization"] == 0.07 and r["seven_day"]["status"] == "allowed"
    assert r["five_hour"]["resets_at"].startswith("2026-10-17T15:00")
    assert U.parse_headers({"content-type": "application/json"}) == {}


def test_should_probe(tmp_path):
    log = U.UsageLog(str(tmp_path / "usage.json"))
    assert not log.should_probe(NOW)                    # nothing known: never opens a window
    log.run("t1", {"m": {"inputTokens": 5}})            # the agent worked: a window is open
    assert log.should_probe()
    log.probed(U.parse_headers(HEADERS))
    assert not log.should_probe()                       # just probed
    later = dt.datetime.now(dt.timezone.utc) + U.PROBE_EVERY + dt.timedelta(seconds=1)
    data = log.load()
    data["limits"]["five_hour"]["resets_at"] = (later + dt.timedelta(hours=1)).isoformat()
    log._save(data)
    assert log.should_probe(later)                      # window still open: keep reading
    data["limits"]["five_hour"]["resets_at"] = (later - dt.timedelta(minutes=1)).isoformat()
    log._save(data)
    assert not log.should_probe(later)                  # window over, agent idle: stop


def test_status_only_event_keeps_the_percentage(tmp_path):
    log = U.UsageLog(str(tmp_path / "usage.json"))
    log.probed(U.parse_headers(HEADERS))
    log.rate_limit({"status": "allowed", "rate_limit_type": "five_hour", "resets_at": RESET})
    lim = log.load()["limits"]["five_hour"]
    assert lim["utilization"] == 0.12 and lim["utilization_at"]
    log.rate_limit({"status": "allowed", "rate_limit_type": "five_hour",
                    "resets_at": RESET + 18000})        # a new window: the old % is gone
    assert log.load()["limits"]["five_hour"]["utilization"] is None


def test_probe_loop(tmp_path, monkeypatch):
    log = U.UsageLog(str(tmp_path / "usage.json"))
    log.run("t1", {"m": {"inputTokens": 5}})
    seen = []
    monkeypatch.setattr(U, "probe", lambda seat: seen.append(seat) or U.parse_headers(HEADERS))

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(U.probe_loop(log, "seat", stop, every=0.01))
        await asyncio.sleep(0.1)
        stop.set()
        await task
    asyncio.run(go())
    assert seen == ["seat"]                             # once, then not before PROBE_EVERY
    assert log.load()["limits"]["five_hour"]["source"] == "probe"


def state(limits, checks=None):
    return {"lab": "t", "at": NOW.isoformat(), "checks": checks or {},
            "agents": {"mgr-a": {"name": "Flash", "role": "manager"}},
            "accounts": {"ana": {"name": "Ana", "agents": ["mgr-a"], "limits": limits,
                                 "tokens_today": {}}}}


def test_web_check_now():
    calls = []

    async def check(human):
        calls.append(human)
    lim = {"five_hour": {"status": "allowed", "utilization": 0.12, "from": "mgr-a",
                         "observed_at": NOW.isoformat()}}
    s = state(lim, {"ana": {"at": NOW.isoformat(), "error": "check failed: boom"}})
    client = TestClient(web.app(lambda: s, check_usage=check))
    page = client.get("/").text
    assert "12% used" in page and "/usage/check/ana" in page and "check failed: boom" in page
    r = client.post("/usage/check/ana", follow_redirects=False)
    assert r.status_code == 303 and calls == ["ana"]
