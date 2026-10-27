"""The lab's web UI (DESIGN.md §9): monitoring only, on the tailnet.

One page, rendered on the server from the secretary's latest snapshot, refreshing itself.
Every reading shows when it was taken and which agent reported it. No external assets.

    GET /                 the page          } as the viewer may see them (visibility.py, #62):
    GET /state.json       the snapshot      } admins all; members the lab, not others' work;
                                            } anyone else machines, states and counts
    GET /logs/{agent}     an agent's event log, for whoever may see it (log_access): admins
                          all; a member their dedicated agents' logs, and their own
                          entries of shared agents (recognised by `tailscale whois`)
"""
import datetime as dt
import html

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

REFRESH_S = 20
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#6b7280;--line:#e3e6eb;--ok:#1f8a4c;
--warn:#b7791f;--bad:#c53030;--bar:#3b82f6;--barbg:#e5e7eb;
--meter:#2a78d6;--good:#0ca30c;--warning:#b7791f;--critical:#d03b3b;
--c1:#2a78d6;--c2:#eb6834;--c3:#1baf7a;--c4:#eda100;--c5:#e87ba4;--c6:#008300}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1a1f27;--fg:#e6e9ef;
--muted:#9aa3b2;--line:#2a313c;--ok:#48bb78;--warn:#ecc94b;--bad:#fc8181;--bar:#60a5fa;
--barbg:#2a313c;--meter:#3987e5;--warning:#fab219;
--c1:#3987e5;--c2:#d95926;--c3:#199e70;--c4:#c98500;--c5:#d55181;--c6:#008300}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;
justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px}
h1{font-size:18px;margin:0}h2{font-size:15px;margin:0 0 10px}
main{padding:20px 24px;display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(360px,1fr))}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
.muted{color:var(--muted)}.small{font-size:12px}.ok{color:var(--ok)}.warn{color:var(--warn)}
.bad{color:var(--bad)}table{width:100%;border-collapse:collapse}
td,th{text-align:left;padding:5px 6px;border-top:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:12px;border-top:none}
.bar{height:8px;background:var(--barbg);border-radius:4px;overflow:hidden;margin:4px 0}
.bar>i{display:block;height:100%;background:var(--bar)}
a{color:inherit}h3{font-size:14px;margin:14px 0 6px}.account+.account{border-top:1px solid var(--line);
margin-top:14px;padding-top:6px}.mblock{margin:12px 0}.mhead{display:flex;
justify-content:space-between;gap:8px}.meter{height:10px;background:var(--barbg);
border-radius:5px;overflow:hidden;margin:6px 0 4px}.meter>i{display:block;height:100%;
border-radius:5px}.mfoot{font-size:13px}.mstatus{margin:6px 0 2px}.mstatus.good{color:var(--good)}
.mstatus.warning,.warning{color:var(--warning)}.mstatus.critical,.critical{color:var(--critical)}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;flex:none}
.tbar{display:flex;gap:2px;height:14px;margin:6px 0 8px}.tbar>i{display:block;height:100%}
.tbar>i:first-child{border-top-left-radius:4px;border-bottom-left-radius:4px}
.tbar>i:last-child{border-top-right-radius:4px;border-bottom-right-radius:4px}
.lrow{display:flex;align-items:baseline;gap:8px;margin:3px 0;flex-wrap:wrap}
.lname{min-width:120px}
a{color:inherit}.pill{display:inline-block;padding:1px 8px;border-radius:999px;border:1px solid var(--line);
font-size:12px}code{font-size:12px;word-break:break-all}
.ahead{display:flex;justify-content:space-between;align-items:center;gap:8px}
.ahead h3{margin-bottom:0}.check{margin:14px 0 0}.check button{font:12px system-ui,sans-serif;
padding:3px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);
color:var(--fg);cursor:pointer}.check button:hover{border-color:var(--meter)}
section.wide{grid-column:1/-1}
.machines td:first-child{width:22%}.machines td:nth-child(2){width:18%}
.agent+.agent{margin-top:6px;padding-top:6px;border-top:1px dashed var(--line)}
.check.inline{display:inline;margin:0 0 0 4px}
.tasks td.what{width:62%}.tasks .tid{font-size:11px;word-break:normal}
.tasks details summary{cursor:pointer;list-style:none}.tasks details summary::-webkit-details-marker{display:none}
.tasks details summary .more{color:var(--meter);font-size:12px;white-space:nowrap}
.tasks details[open] summary .more{display:none}
.tasks .full{white-space:pre-wrap;word-break:break-word;margin:6px 0 2px;font-size:13px}
.tasks .st{white-space:nowrap}.tasks .detail{font-size:12px;color:var(--muted);margin-top:2px;
word-break:break-word}
@media (max-width:420px){main{padding:12px;grid-template-columns:1fr}header{padding:14px 16px}
.tasks td.what{width:auto}}
"""

LIMIT_NAMES = {"five_hour": "5-hour window", "seven_day": "weekly", "seven_day_opus":
               "weekly (Opus)", "seven_day_sonnet": "weekly (Sonnet)", "overage": "overage"}
STATE_CLASS = {"done": "ok", "running": "", "queued": "muted", "awaiting_approval": "warn",
               "waiting": "",
               "failed": "bad", "cancelled": "muted"}


def e(x):
    return html.escape("" if x is None else str(x))


def ago(iso, now=None):
    """'3 min ago' for an ISO timestamp."""
    if not iso:
        return "never"
    try:
        t = dt.datetime.fromisoformat(iso)
    except ValueError:
        return iso
    now = now or dt.datetime.now(dt.timezone.utc)
    s = int((now - t).total_seconds())
    if s < 90:
        return f"{max(s, 0)} s ago"
    if s < 5400:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s // 3600} h ago"
    return f"{s // 86400} days ago"


WINDOWS = [("five_hour", "Current session", "5-hour window"),
           ("seven_day", "Weekly", "all models"),
           ("seven_day_opus", "Weekly", "Opus"),
           ("seven_day_sonnet", "Weekly", "Sonnet")]
STATUS = {"allowed": ("✓", "Within limit", "good"),
          "allowed_warning": ("⚠", "Close to the limit", "warning"),
          "rejected": ("⛔", "Limit reached", "critical")}


def resets_in(iso, now=None):
    """'2 h 52 min' until an ISO time."""
    if not iso:
        return None
    try:
        t = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    s = int((t - (now or dt.datetime.now(dt.timezone.utc))).total_seconds())
    if s <= 0:
        return "now"
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d} d {h} h" if d else (f"{h} h {m} min" if h else f"{m} min")


def human(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else (f"{n / 1e3:.0f}k" if n >= 1e3 else str(n))


def _meter(kind, lim):
    title, sub = next(((t, s) for k, t, s in WINDOWS if k == kind), (kind, ""))
    icon, word, role = STATUS.get(lim.get("status"), ("", lim.get("status") or "", "good"))
    util = lim.get("utilization")
    left = resets_in(lim.get("resets_at"))
    head = (f"<div class='mhead'><span><b>{e(title)}</b> <span class='muted small'>{e(sub)}"
            f"</span></span><span class='small'>{f'resets in {e(left)}' if left else ''}</span></div>")
    if util is None:
        # only the status came with this reading: no percentage to draw
        body = (f"<div class='mstatus {role}'>{icon} {e(word)} "
                f"<span class='muted small'>(no percentage reported)</span></div>")
    else:
        pct = max(0, min(100, round(100 * util)))
        fill = "var(--meter)" if role == "good" else f"var(--{role})"
        body = (f"<div class='meter' role='meter' aria-valuenow='{pct}' aria-valuemin='0' "
                f"aria-valuemax='100' title='{pct}% used'><i style='width:{pct}%;background:{fill}'>"
                f"</i></div><div class='mfoot'><b>{pct}% used</b>"
                + (f" <span class='{role}'>{icon} {e(word)}</span>" if role != "good" else "")
                + "</div>")
    seen = (f"<div class='muted small'>as of "
            f"{e(ago(lim.get('utilization_at') or lim.get('observed_at')))}, "
            f"from {e(lim.get('from'))}</div>")
    return f"<div class='mblock'>{head}{body}{seen}</div>"


SLOTS = 6     # categorical slots; beyond that, agents fold into "other"


def _tokens(state, acc):
    """Today's tokens of the whole account, one bar split by agent: a share of the seat's
    use, not of its limit. An agent's colour follows its place in lab.yaml, never its rank."""
    rows = acc["tokens_today"]
    if not rows:
        return "<p class='muted small'>No tokens used today.</p>"
    norm = {a: (v if isinstance(v, dict) else {"new": v, "cached": 0}) for a, v in rows.items()}
    order = [a for a in acc["agents"] if a in norm] + [a for a in norm if a not in acc["agents"]]
    slots, other = order[:SLOTS], order[SLOTS:]
    parts = [(a, norm[a], f"var(--c{k + 1})") for k, a in enumerate(slots)]
    if other:
        parts.append(("other", {"new": sum(norm[a]["new"] for a in other),
                                "cached": sum(norm[a]["cached"] for a in other)}, "var(--muted)"))
    total = sum(v["new"] + v["cached"] for _, v, _ in parts) or 1
    segs, legend = [], []
    for aid, v, color in parts:
        n = v["new"] + v["cached"]
        if not n:
            continue
        name = state["agents"].get(aid, {}).get("name", aid)
        pct = 100 * n / total
        tip = f"{name}: {n:,} tokens ({v['new']:,} new, {v['cached']:,} cache reads)"
        segs.append(f"<i style='width:{pct:.2f}%;background:{color}' title='{e(tip)}'></i>")
        legend.append(f"<div class='lrow' title='{e(tip)}'><i class='sw' style='background:"
                      f"{color}'></i><span class='lname'>{e(name)}</span><b>{human(n)}</b>"
                      f"<span class='muted small'>{pct:.0f}% · {human(v['new'])} new, "
                      f"{human(v['cached'])} cache reads</span></div>")
    return (f"<h3>Tokens today <span class='muted small'>{human(total)} on this account, "
            f"by agent</span></h3><div class='tbar'>{''.join(segs)}</div>"
            f"<div class='tlegend small'>{''.join(legend)}</div>"
            f"<div class='muted small'>Cache reads are re-reads of context: cheap, and they "
            f"inflate totals.</div>")


def _accounts(state):
    cards, quiet = [], []
    for hid, acc in state["accounts"].items():
        if not acc["limits"] and not any((acc.get("tokens_today") or {}).values()):
            quiet.append((hid, acc))        # nothing to show: one line below
            continue
        agents = ", ".join(e(state["agents"][a]["name"]) for a in acc["agents"]) or "—"
        order = [k for k, _, _ in WINDOWS]
        meters = [_meter(k, acc["limits"][k]) for k in sorted(
            acc["limits"], key=lambda k: order.index(k) if k in order else 99)]
        if not meters:
            meters = ["<p class='muted small'>No limit readings yet: they arrive while an "
                      "agent on this seat is running, or press <i>Check now</i>.</p>"]
        check = (f"<form method='post' action='/usage/check/{e(hid)}' class='check'>"
                 f"<button title='Ask an agent on this seat to read its limits now (one tiny "
                 f"request; may start a 5-hour window if none is open)'>Check now</button>"
                 f"</form>") if acc["agents"] else ""
        note = state.get("checks", {}).get(hid)
        note = (f"<div class='small {'bad' if note.get('error') else 'muted'}'>"
                f"{e(note.get('error') or 'checked ' + ago(note.get('at')))}</div>") if note else ""
        cards.append(f"<div class='account'><div class='ahead'><h3>{e(acc['name'])}</h3>{check}"
                     f"</div><div class='muted small'>seat used by: {agents}</div>{note}"
                     f"{''.join(meters)}{_tokens(state, acc)}</div>")
    if quiet:
        cards.append("<div class='account'><div class='muted small'>No readings yet for "
                     + ", ".join(
                         f"{e(acc['name'])}"
                         + (f" <form method='post' action='/usage/check/{e(hid)}' class='check "
                            f"inline'><button>Check now</button></form>" if acc["agents"] else "")
                         for hid, acc in quiet)
                     + ": they arrive while an agent on the seat runs.</div></div>")
    return "<section><h2>Claude usage, by account</h2>" + "".join(cards) + "</section>"


def _gpu_line(a):
    """'NVIDIA H100 80GB HBM3: 87% · 61/80 GB · 2 processes', one part per GPU."""
    if not a.get("gpus"):
        return a.get("gpu") or "none"
    parts = [f"{g['name']}: {g.get('util')}% · {round((g.get('mem_used_mb') or 0) / 1024)}/"
             f"{round((g.get('mem_total_mb') or 0) / 1024)} GB" for g in a["gpus"]]
    n = len(a.get("gpu_processes") or [])
    return "; ".join(parts) + (f" · {n} process{'es' if n > 1 else ''}" if n else "")


def _workers(state):
    out = []
    for aid, a in state["agents"].items():
        if a.get("role") != "worker":
            continue
        if not a.get("up"):
            st = _st(a)
            word = st["word"] if st["word"] != "—" else "down"
            out.append(f"<h2>{e(a['name'])} <span class='pill {e(st.get('level') or 'bad')}'>"
                       f"{e(word)}</span></h2>"
                       + (f"<p class='muted small'>{e(st.get('hint'))}</p>" if st.get("hint")
                          else f"<p class='muted small'>{e(a.get('error'))}</p>"))
            continue
        q = a.get("queue") or {}
        busy = q.get("running", 0) + q.get("awaiting_approval", 0) + q.get("waiting", 0)
        why = ", ".join(f"{q[k]} {k.replace('_', ' ')}" for k in
                        ("running", "awaiting_approval", "waiting", "queued") if q.get(k))
        status = (f"<span class='pill warn'>busy: {e(why)}</span>" if busy else
                  "<span class='pill ok'>idle</span>"
                  + (f" <span class='muted small'>{e(why)}</span>" if why else ""))
        rows = "".join(_task_row(t) for t in a.get("recent") or [])
        out.append(f"<h2>{e(a['name'])} {status}</h2>"
                   + "".join(f"<div class='small warn'>kept awake by {e(h['user'])}"
                             f"{' (' + e(h['note']) + ')' if h.get('note') else ''} "
                             f"(~/.lab-keep-awake): never stopped while it's there</div>"
                             for h in a.get("keep_awake") or []) +
                   "".join(f"<div class='small'>long run <code>{e(r.get('id'))}</code> "
                           f"{e(r.get('name'))} for task <code>{e(r.get('task'))}</code>, "
                           f"since {e(ago(r.get('started')) if r.get('started') else '…')}</div>"
                           for r in a.get("runs") or []) +
                   f"<div class='small'>GPU: {e(_gpu_line(a))} · disk free "
                   f"{e(a.get('disk_free_gb'))} GB</div>"
                   f"<table class='tasks'><tr><th>Task</th><th>State</th><th>For</th></tr>"
                   f"{rows or '<tr><td class=muted colspan=3>no tasks yet</td></tr>'}</table>")
    return ("<section class='wide'>" + ("".join(out) or "<h2>Workers' tasks</h2><p class=muted>no workers</p>")
            + "</section>")


SHORT = 140         # a task's instruction before "more": the whole of it is one click away


def _clock(iso):
    """'16:03 UTC' (today) or 'Oct 26 16:03 UTC'."""
    import datetime as dt
    try:
        t = dt.datetime.fromisoformat(iso).astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        return ""
    today = dt.datetime.now(dt.timezone.utc).date()
    return f"{t:%H:%M} UTC" if t.date() == today else f"{t:%b %d %H:%M} UTC"


def _for(iso):
    """'14 min' for how long since `iso`."""
    return ago(iso).replace(" ago", "") if iso else "…"


def _when(t):
    """What a task's state means for it right now, in words."""
    st, w = t.get("state"), t.get("waiting") or {}
    if st == "running":
        return f"running for {_for(t['started'])}" if t.get("started") else "running"
    if st == "awaiting_approval":
        return "waiting for a human's approval (Slack)"
    if st == "waiting":
        bits = [f"waiting since {ago(w['since'])}" if w.get("since") else "waiting for its runs"]
        if w.get("wake_at"):
            bits.append(f"check-in {_clock(w['wake_at'])}")
        if w.get("runs"):
            bits.append("runs " + ", ".join(w["runs"]))
        return "; ".join(bits)
    if st == "queued":
        return f"queued {ago(t.get('created'))}"
    if st in ("done", "failed", "cancelled"):
        return f"{st} {ago(t.get('finished'))}" if t.get("finished") else st
    return st or ""


def _task_row(t):
    """One task: what it is (all of it on a click), what its state means now, and who for.
    The id is small: it's for logs and `lab debug`, not for reading."""
    text = (t.get("instruction") or "").strip()
    first, rest = text[:SHORT], text[SHORT:]
    what = (f"<details><summary>{e(first)}{'… <span class=more>more</span>' if rest else ''}"
            f"</summary><div class='full'>{e(text)}</div></details>" if rest else e(first)) \
        if text else "<span class='muted'>(details hidden)</span>"
    extra = ""
    if t.get("state") == "waiting" and (t.get("waiting") or {}).get("note"):
        extra = f"<div class='detail'>{e(t['waiting']['note'])}</div>"
    elif t.get("state") == "failed" and t.get("error"):
        extra = f"<div class='detail bad'>{e(t['error'])}</div>"
    if t.get("last_progress") and t.get("state") in ("running", "awaiting_approval", "waiting"):
        extra += (f"<div class='detail'>latest: {e(t['last_progress'])}"
                  f"{' (' + e(ago(t['last_progress_at'])) + ')' if t.get('last_progress_at') else ''}"
                  "</div>")
    return (f"<tr><td class='what'>{what}<div class='muted tid'><code>{e(t.get('id'))}</code>"
            f"</div></td>"
            f"<td><span class='st {STATE_CLASS.get(t.get('state'), '')}'>{e(t.get('state'))}"
            f"</span><div class='detail'>{e(_when(t))}</div>{extra}</td>"
            f"<td>{e(t.get('requester'))}<div class='muted small'>via {e(t.get('manager'))}"
            f"</div></td></tr>")


def timeout_text(t, tz=None):
    """A member's approval timeout in words, the same in Slack and on this page: the value,
    where it comes from, and an override's end in the member's own time zone (`tz`, a
    tzinfo or a zone name) and UTC. `t`: the broker's {timeout_s, by, until}."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    s = (t or {}).get("timeout_s")
    value = f"{s // 60} minutes" if s else "none"
    if not (t or {}).get("by"):
        return f"{value} (lab.yaml)"
    until = ""
    if t.get("until"):
        utc = dt.datetime.fromisoformat(t["until"]).astimezone(dt.timezone.utc)
        try:
            zone = ZoneInfo(tz) if isinstance(tz, str) else tz
        except (ValueError, KeyError, OSError):
            zone = None
        if zone is None:
            until = f", until {utc:%Y-%m-%d %H:%M} UTC; their Slack time zone isn't known"
        else:
            local = utc.astimezone(zone)
            until = f", until {local:%Y-%m-%d %H:%M} {local.tzname()} ({utc:%H:%M} UTC)"
    return f"{value} (set by {t['by']}{until})"


def _timeouts(state):
    """Members' approval timeouts (#77): how long their requests wait before a timeout."""
    ts = (state.get("approvals") or {}).get("timeouts")
    if not ts:
        return ""
    rows = "".join(f"<tr><td>{e(h)}</td><td>{e(timeout_text(t, t.get('tz')))}</td></tr>"
                   for h, t in sorted(ts.items()))
    return ("<h2 style='margin-top:14px'>Approval timeouts</h2><table><tr><th>Member</th>"
            "<th>Waits, then a timeout (denied)</th></tr>" + rows + "</table>")


def _approvals(state):
    ap = state.get("approvals") or {}
    if ap.get("error"):
        return f"<section><h2>Approvals</h2><p class='muted'>{e(ap['error'])}</p></section>"
    pend = "".join(f"<tr><td>{e(r['agent'])}</td><td><code>{e(r['detail'][:120])}</code></td>"
                   f"<td>{e(r['human'])}</td><td class='muted small'>until "
                   f"{e(r['expires'][11:16]) + ' UTC' if r.get('expires') else 'no timeout'}</td></tr>"
                   for r in ap.get("pending") or [])
    grants = "".join(
        f"<tr><td>{e(agent)}</td><td>{e(kind)} <b>{e(value)}</b>"
        + (f" <span class='muted small'>×{len(gs)}</span>" if len(gs) > 1 else "")
        + f"</td><td>{e(', '.join(sorted({g.get('granted_by') or '?' for g in gs})))}</td>"
        f"<td class='muted small'>until {e(_clock(max(g['expires'] for g in gs)))}</td></tr>"
        for (agent, kind, value), gs in _grouped(ap.get("grants") or {}))
    return ("<section><h2>Approvals</h2><table><tr><th>Agent</th><th>Asks</th><th>Who decides"
            "</th><th></th></tr>" + (pend or "<tr><td class=muted colspan=4>nothing pending"
                                     "</td></tr>") +
            "</table><h2 style='margin-top:14px'>Active grants</h2><table><tr><th>Agent</th>"
            "<th>Grant</th><th>Given by</th><th></th></tr>" +
            (grants or "<tr><td class=muted colspan=4>none</td></tr>") + "</table>" +
            _timeouts(state) + "</section>")


def _grouped(grants):
    """Grants as ((agent, kind, value), [grants]) sorted by agent and value: the same grant
    given several times is one row."""
    out = {}
    for agent, gs in grants.items():
        for g in gs:
            out.setdefault((agent, g.get("kind"), g.get("value")), []).append(g)
    return sorted(out.items(), key=lambda kv: (kv[0][0], str(kv[0][1]), str(kv[0][2])))


def _machines(state):
    """Every machine with what runs on it: its power and idle policy, and each agent there
    with its status (one status for all views, secretary/overview.py)."""
    hosts, agents = state.get("hosts") or {}, state.get("agents") or {}
    names = list(dict.fromkeys(list(hosts) + [a.get("host") for a in agents.values()
                                             if a.get("host")]))
    homeless = [aid for aid, a in agents.items() if not a.get("host")]
    if not names and not homeless:
        return ""
    rows = []
    for h in names:
        v = hosts.get(h) or {}
        policy = (f"idle stop {v['idle_stop_min']} min" if v.get("idle_stop_min")
                  else "no idle stop")
        if v.get("idle_since"):
            policy += f" · idle since {ago(v['idle_since'])}"
        here = "".join(_agent_line(aid, a) for aid, a in agents.items() if a.get("host") == h)
        rows.append(
            f"<tr><td><b>{e(h)}</b><div class='muted small'>{e(policy)}</div>"
            + (f"<div class='small {'bad' if v['stale'].get('gave_up') else 'warn'}'>"
               f"{e(stale_text(v['stale']))}</div>" if v.get("stale") else "")
            + f"</td><td><span class='{e(_st(v)['level'])}'>{e(_st(v)['word'])}</span>"
            + (f"<div class='muted small'>{e(_st(v)['hint'])}</div>" if _st(v).get("hint") else "")
            + f"</td><td>{here or '<span class=muted>no agents</span>'}</td></tr>")
    if homeless:                # never left out: an agent whose machine isn't known
        rows.append("<tr><td class='muted'>(machine unknown)</td><td></td><td>"
                    + "".join(_agent_line(aid, agents[aid]) for aid in homeless) + "</td></tr>")
    return ("<section class='wide'><h2>Machines and agents</h2><table class='machines'><tr>"
            "<th>Machine</th><th>Power</th><th>Agents</th></tr>" + "".join(rows)
            + "</table></section>")


def _agent_line(aid, a):
    """One agent on its machine: name (its log), role and seat, status and why."""
    st = _st(a)
    who = " · ".join(x for x in (a.get("role"), f"seat {a['human']}" if a.get("human") else "",
                                 aid if a.get("name") and a.get("name") != aid else "") if x)
    why = " · ".join(x for x in (st.get("hint"), st.get("reason"),
                                 f"since {ago(st['since'])}" if st.get("since") else "") if x)
    q = a.get("queue") or {}
    queue = ", ".join(f"{q[k]} {k.replace('_', ' ')}" for k in
                      ("running", "awaiting_approval", "waiting", "queued") if q.get(k))
    return (f"<div class='agent'><b><a href='/logs/{e(aid)}'>{e(a.get('name') or aid)}</a></b> "
            f"<span class='muted small'>{e(who)}</span> "
            f"<span class='{e(st['level'])}'>{e(st['word'])}</span>"
            + (f"<div class='muted small'>{e(why)}</div>" if why else "")
            + (f"<div class='small'>tasks: {e(queue)}</div>" if queue else "")
            + "".join(f"<div class='small bad'>⚠ GitHub {e(n)}: {e(p)}</div>"
                      for n, p in (a.get("github") or {}).items())
            + "</div>")


def stale_text(s):
    """'stale: config 3 commits behind, framework older' for a host that missed deploys."""
    n = s.get("config_behind")
    parts = [f"config {n} commit{'s' if n != 1 else ''} behind" if n else
             "config differs" if n is None else "",
             "framework differs" if s.get("framework_differs") else ""]
    return ("stale: " + ", ".join(p for p in parts if p)
            + (" · catch-up gave up: deploy by hand" if s.get("gave_up") else "")
            + (f" · catch-up blocked: {s['blocked']}" if s.get("blocked") else ""))


def _st(x):
    """A host's or agent's one status (secretary/overview.py), or a neutral one."""
    return x.get("status") or {"word": "—", "level": "muted"}


def render(state):
    if state is None:
        body = "<main><section><p>Collecting the lab's state…</p></section></main>"
        title, when = "Agentic Lab", ""
    else:
        title = f"{state['lab']} · Agentic Lab"
        ap = state.get("applied") or {}
        when = f"updated {ago(state['at'])}" + (
            f" · config {str(ap.get('config_commit'))[:7]}"
            f"{' (UNREVIEWED)' if ap.get('unreviewed') else ''}"
            f" · framework {str(ap.get('framework_commit'))[:7]}" if ap.get("config_commit") else "")
        # what runs where first, then the work, what waits for a human, and usage last
        body = ("<main>" + _machines(state) + _workers(state) + _approvals(state) +
                _accounts(state) + "</main>")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{e(title)}</title>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<meta http-equiv='refresh' content='{REFRESH_S}'><style>{CSS}</style></head>"
            f"<body><header><h1>{e(title)}</h1><span class='muted small'>{e(when)} · "
            f"monitoring only; approvals happen in Slack</span></header>{body}</body></html>")


KIND_CLASS = {"message": "", "reply": "ok", "tool": "muted", "result": "muted",
              "approval": "warn", "task": "", "error": "bad"}
HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
           "Cache-Control": "no-store"}


def log_access(lab, member, agent):
    """What `member` may see of `agent`'s log: "all", "own" (only their own entries), or
    None. Admins: everything. A member: their dedicated agents' whole log (guest turns in
    their manager's conversations included); of shared agents and the secretary (whose logs
    hold other members' work), their own entries only."""
    if member is None or member not in lab.humans or agent not in lab.agents:
        return None
    if (lab.humans.get(member) or {}).get("admin"):
        return "all"
    a = lab.agents[agent]
    if a.get("role") in ("manager", "worker") and a.get("human") == member \
            and a.get("access", "dedicated") == "dedicated":
        return "all"
    if a.get("role") == "secretary" or a.get("access") == "shared":
        return "own"
    return None


def own_entries(events, member, task_ids=()):
    """A member's own entries of a shared log: their messages, and their tasks' events."""
    mine = set(task_ids)
    return [ev for ev in events if ev.get("member") == member or ev.get("task") in mine]


def render_logs(state, agent, hours, events, error=None, scope="all"):
    name = (state or {}).get("agents", {}).get(agent, {}).get("name", agent)
    picks = " · ".join(f"<a href='/logs/{e(agent)}?hours={h}'>{label}</a>"
                       for h, label in ((1, "1 h"), (24, "24 h"), (168, "7 days")))
    rows = "".join(
        f"<tr><td class='muted small' style='white-space:nowrap'>{e(ev.get('at', '')[5:16].replace('T', ' '))}</td>"
        f"<td class='{KIND_CLASS.get(ev.get('kind'), '')} small'>{e(ev.get('kind'))}"
        f"{' ⚠' if ev.get('error') else ''}</td>"
        f"<td class='small muted'>{e(ev.get('task') or ev.get('member') or '')}</td>"
        f"<td><pre style='margin:0;white-space:pre-wrap;font:12px/1.4 ui-monospace,monospace'>"
        f"{e(ev.get('text'))}</pre></td></tr>"
        for ev in reversed(events))
    body = (f"<main style='display:block'><section><h2>{e(name)}: the last {e(hours)} h</h2>"
            f"<p class='small'><a href='/'>← the lab</a> · {picks}</p>"
            + (f"<p class='bad'>{e(error)}</p>" if error else "") +
            f"<table><tr><th>When (UTC)</th><th>What</th><th>Task / member</th><th></th></tr>"
            f"{rows or '<tr><td class=muted colspan=4>nothing in this period</td></tr>'}</table>"
            f"</section></main>")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{e(name)} · logs</title>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<style>{CSS}</style></head><body><header><h1>{e(name)} · log</h1>"
            f"<span class='muted small'>{'all entries' if scope == 'all' else 'your entries only'}"
            f"</span></header>{body}</body></html>")


def app(get_state, get_events=None, viewer=None, check_usage=None, access=None,
        may_check=None, shape=None):
    """`get_events(agent, hours, member)` (async) -> events (only `member`'s own entries
    when given); `viewer(client_ip)` (async) -> the member behind that address, or None;
    `access(member, agent)` -> "all" | "own" | None (log_access); `check_usage(human)`
    (async): read that seat's limits now; `shape(state, member)`: the state as that viewer
    (None: anonymous) may see it."""
    async def seen(request):
        state = get_state()
        if shape is None:
            return state
        member = await viewer(request.client.host) if viewer is not None else None
        return shape(state, member)

    async def check(request):
        """Read a seat's limits now: only for the member themselves (or an admin), whom
        the request's Tailscale identity names (#62); it spends a request on that seat."""
        human = request.path_params["human"]
        member = await viewer(request.client.host) if viewer is not None else None
        if may_check is None or not may_check(member, human):
            return HTMLResponse("<!doctype html><p>Only that member (or an admin) can check "
                                "their seat.</p>", status_code=403, headers=HEADERS)
        if check_usage is not None:
            await check_usage(human)
        return RedirectResponse("/", status_code=303)

    async def icon(request):
        """An agent's generated Slack app icon (or 'approvals'), to download and upload."""
        from ..icon import png
        agent = request.path_params["agent"]
        if agent in ("approvals", "broker"):
            name = "Lab Approvals"
        else:
            name = ((get_state() or {}).get("agents") or {}).get(agent, {}).get("name")
            if not name:
                return HTMLResponse("unknown agent", status_code=404, headers=HEADERS)
        return Response(png(name), media_type="image/png", headers={
            "Content-Disposition": f'attachment; filename="{agent}-icon.png"'})

    async def page(request):
        return HTMLResponse(render(await seen(request)), headers=HEADERS)

    async def state_json(request):
        return JSONResponse(await seen(request) or {}, headers=HEADERS)

    async def logs(request):
        agent = request.path_params["agent"]
        member = await viewer(request.client.host) if viewer is not None else None
        scope = access(member, agent) if access is not None and member else None
        if get_events is None or scope is None:
            return HTMLResponse("<!doctype html><p>You can see the logs of your own agents "
                                "(admins: every agent's). Your Tailscale login must be in "
                                "lab.yaml (tailscale_login).</p>",
                                status_code=403, headers=HEADERS)
        try:
            hours = min(max(float(request.query_params.get("hours", 24)), 0.1), 336)
        except ValueError:
            hours = 24
        try:
            events, error = await get_events(agent, hours,
                                             member if scope == "own" else None), None
        except Exception as ex:
            events, error = [], f"couldn't fetch the log: {ex}"
        return HTMLResponse(render_logs(get_state(), agent, hours, events, error, scope),
                            headers=HEADERS)

    return Starlette(routes=[Route("/", page), Route("/state.json", state_json),
                             Route("/logs/{agent}", logs),
                             Route("/usage/check/{human}", check, methods=["POST"]),
                             Route("/icon/{agent}.png", icon)])
