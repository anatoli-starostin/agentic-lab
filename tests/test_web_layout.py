"""The dashboard's layout: machines with their agents first, a worker's tasks readable in
full, one row per grant, quiet accounts in one line, and one status everywhere."""
import datetime as dt

from agentic_lab.secretary import visibility as V
from agentic_lab.secretary import web
from agentic_lab.secretary.status import task_summary

NOW = dt.datetime.now(dt.timezone.utc)
AGO = lambda **kw: (NOW - dt.timedelta(**kw)).isoformat()      # noqa: E731
LONG = "Train the model. " * 30                                 # longer than SHORT


def task(tid, state, **kw):
    return {"id": tid, "state": state, "requester": "alice", "manager": "mgr",
            "created": AGO(minutes=30), "instruction": LONG, **kw}


STATE = {
    "lab": "dev", "at": NOW.isoformat(),
    "hosts": {"gpu-a": {"state": "RUNNING", "idle_stop_min": 30,
                        "status": {"word": "running", "level": "ok", "hint": ""}},
              "gpu-b": {"state": "STOPPED", "idle_stop_min": 30,
                        "status": {"word": "stopped", "level": "muted",
                                   "hint": "ask the secretary to power it on"}},
              "mgr-vm": {"state": "RUNNING", "idle_stop_min": 60,
                         "status": {"word": "running", "level": "ok", "hint": ""}}},
    "agents": {
        "worker-a": {"name": "worker-a", "role": "worker", "human": "alice", "host": "gpu-a",
                     "up": True, "queue": {"running": 1, "waiting": 1, "queued": 1},
                     "status": {"word": "up", "level": "ok"},
                     "recent": [
                         task("t-run", "running", started=AGO(minutes=14),
                              last_progress="step 300/500", last_progress_at=AGO(minutes=2)),
                         task("t-wait", "waiting", waiting={
                             "note": "healthy at step 3940", "since": AGO(hours=2),
                             "wake_at": (NOW + dt.timedelta(hours=1)).isoformat(),
                             "runs": ["r-abc"]}),
                         task("t-q", "queued"),
                         task("t-bad", "failed", finished=AGO(hours=2), error="no LaTeX"),
                         task("t-ok", "done", finished=AGO(minutes=41), instruction="short one")]},
        "worker-b": {"name": "worker-b", "role": "worker", "human": "bob", "host": "gpu-b",
                     "up": False, "error": "unreachable: ConnectTimeout",
                     "status": {"word": "host stopped", "level": "muted",
                                "hint": "ask the secretary to power it on"}},
        "mgr": {"name": "Ada", "role": "manager", "human": "alice", "host": "mgr-vm",
                "status": {"word": "up", "level": "ok", "reason": "busy: follows 2 tasks"}},
    },
    "accounts": {"alice": {"name": "Alice", "agents": ["worker-a", "mgr"], "limits": {},
                           "tokens_today": {"worker-a": {"new": 10, "cached": 5}}},
                 "bob": {"name": "Bob", "agents": ["worker-b"], "limits": {},
                         "tokens_today": {}},
                 "shared": {"name": "Shared", "agents": [], "limits": {}, "tokens_today": {}}},
    "approvals": {"pending": [], "grants": {"worker-a": [
        {"kind": "domain", "value": "api.wandb.ai", "granted_by": "alice",
         "expires": (NOW + dt.timedelta(hours=h)).isoformat()} for h in (1, 3, 2)]}},
}


def test_the_page_starts_with_machines_and_ends_with_usage():
    page = web.render(STATE)
    order = [page.index(x) for x in ("Machines and agents", "worker-a <span",
                                     "Approvals", "Claude usage")]
    assert order == sorted(order)


def test_each_machine_lists_its_agents_with_their_status():
    page = web.render(STATE)
    machines = page[page.index("Machines and agents"):page.index("worker-a <span")]
    assert machines.index("gpu-a") < machines.index("/logs/worker-a") < machines.index("gpu-b")
    assert "tasks: 1 running, 1 waiting, 1 queued" in machines
    assert "busy: follows 2 tasks" in machines and "idle stop 60 min" in machines


def test_a_stopped_workers_host_is_not_called_down():
    page = web.render(STATE)
    assert "host stopped" in page and "ConnectTimeout" not in page
    assert "<span class='pill bad'>down</span>" not in page


def test_tasks_show_what_their_state_means_and_their_whole_instruction():
    page = web.render(STATE)
    assert "busy: 1 running, 1 waiting, 1 queued" in page
    assert "running for 14 min" in page and "latest: step 300/500 (2 min ago)" in page
    assert "waiting since 2 h ago" in page and "check-in" in page and "runs r-abc" in page
    assert "healthy at step 3940" in page
    assert "queued 30 min ago" in page and "done 41 min ago" in page
    assert "no LaTeX" in page and "failed 2 h ago" in page
    assert "<details>" in page and LONG.strip() in page        # all of it, a click away
    assert "short one" in page                                  # short: shown as is
    assert "<th>When</th>" not in page


def test_a_grant_given_several_times_is_one_row_with_its_latest_expiry():
    page = web.render(STATE)
    assert page.count("<b>api.wandb.ai</b>") == 1 and "×3" in page
    latest = (NOW + dt.timedelta(hours=3)).astimezone(dt.timezone.utc)
    assert f"{latest:%H:%M} UTC" in page


def test_accounts_without_readings_share_one_line():
    page = web.render(STATE)
    usage = page[page.index("Claude usage"):]
    assert "No readings yet for Bob" in usage and "Shared" in usage
    assert usage.count("<div class='ahead'>") == 1      # only Alice has a card


def test_another_members_tasks_stay_hidden():
    t = task_summary({**task("t-x", "waiting", started=AGO(hours=1)), "progress": [
        {"at": AGO(minutes=1), "message": "SECRET-PROGRESS"}], "error": None,
        "waiting": {"note": "SECRET-NOTE", "since": AGO(hours=1)}})
    seen = V.task_view(t, "bob")
    assert set(seen) <= set(V.TASK_PUBLIC)
    page = web.render({**STATE, "agents": {**STATE["agents"], "worker-a": {
        **STATE["agents"]["worker-a"], "recent": [seen]}}})
    assert "SECRET" not in page and "Train the model" not in page
    assert "(details hidden)" in page
