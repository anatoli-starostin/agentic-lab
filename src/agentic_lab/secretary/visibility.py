"""What each viewer sees of the lab (#62): transparent about the lab, private about the work.

    admins (and every member with `visibility: team` in lab.yaml)    everything
    a member    what the lab is doing: machines, busy or idle and why (whose task, who keeps
                a machine awake), queues, task states and owners, Claude usage per seat;
                of other members' work no instructions, results, files or approval details
    anyone else (an unknown tailnet device, a lab host, i.e. an agent)
                machines, states and counts: no names, no text

The same shaping applies to the web UI (`/`, `/state.json`), the Slack Home tab, and the
secretary's tools (`lab_state`, `worker_tasks`, `task_result`).
"""
import copy

ALL, MEMBER, NOBODY = "all", "member", "nobody"
VISIBILITY = ("private", "team")
TASK_PUBLIC = ("id", "state", "requester", "manager", "created", "finished")


def level(lab, member):
    if member is None or member not in lab.humans:
        return NOBODY
    if (lab.humans.get(member) or {}).get("admin") or lab.data.get("visibility") == "team":
        return ALL
    return MEMBER


def sees_all(lab, member):
    return level(lab, member) == ALL


def task_view(task, member):
    """A task as `member` may see it (not an admin): their own in full, others' state only."""
    if task.get("requester") == member:
        return task
    return {k: task.get(k) for k in TASK_PUBLIC if k in task}


def shape(state, lab, member):
    """`state` (the secretary's snapshot) as `member` (None: anonymous) may see it."""
    if not state:
        return state
    lv = level(lab, member)
    if lv == ALL:
        return state
    s = copy.deepcopy(state)
    return _for_member(s, member) if lv == MEMBER else _for_nobody(s)


def _for_member(s, member):
    for a in (s.get("agents") or {}).values():
        if a.get("recent"):
            a["recent"] = [task_view(t, member) for t in a["recent"]]
        mine = {t["id"] for t in a.get("recent") or [] if t.get("requester") == member}
        for r in a.get("runs") or []:
            if r.get("requester") != member and r.get("task") not in mine:
                r["name"] = None                # a run's name can say what the work is
    ap = s.get("approvals") or {}
    agents = s.get("agents") or {}
    for aid, grants in (ap.get("grants") or {}).items():
        a = agents.get(aid) or {}
        if a.get("access", "dedicated") != "shared" and a.get("human") != member:
            # another member's own agent: what kinds of grants, not what they name
            ap["grants"][aid] = [{"kind": g.get("kind"), "value": "(another member's)",
                                  "expires": g.get("expires"), "granted_by": g.get("granted_by")}
                                 for g in grants]
    ap["pending"] = [r if r.get("human") == member else
                     {**{k: r.get(k) for k in ("id", "agent", "human", "expires")},
                      "detail": "(another member's request)"}
                     for r in ap.get("pending") or []]
    return s


def _for_nobody(s):
    agents, n = {}, {}
    for a in (s.get("agents") or {}).values():
        role = a.get("role") or "agent"
        n[role] = n.get(role, 0) + 1
        key = f"{role}-{n[role]}"
        agents[key] = {
            "id": key, "name": f"{role} {n[role]}" + (f" on {a['host']}" if a.get("host") else ""),
            "role": role, "host": a.get("host"), "up": a.get("up"),
            "service": a.get("service"), "queue": a.get("queue"), "gpu": a.get("gpu"),
            "gpus": a.get("gpus"), "gpu_processes": [{} for _ in a.get("gpu_processes") or []],
            "disk_free_gb": a.get("disk_free_gb"),
            "keep_awake": [{"user": "someone"} for _ in a.get("keep_awake") or []],
            "recent": [{"id": "", "state": t.get("state"), "requester": "", "manager": "",
                        "created": t.get("created"), "finished": t.get("finished")}
                       for t in a.get("recent") or []]}
    ap = s.get("approvals") or {}
    return {"lab": s.get("lab"), "at": s.get("at"), "agents": agents, "accounts": {},
            "approvals": {"pending": [{"agent": "", "human": "", "detail": "",
                                       "expires": r.get("expires")}
                                      for r in ap.get("pending") or []],
                          "grants": {}} if not ap.get("error") else {"error": "unavailable"},
            "hosts": {h: {k: v.get(k) for k in ("state", "at", "idle_stop_min", "idle_since")}
                      for h, v in (s.get("hosts") or {}).items()},
            "applied": s.get("applied")}
