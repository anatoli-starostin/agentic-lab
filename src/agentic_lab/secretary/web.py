"""The lab's web UI (DESIGN.md §9): monitoring only, on the tailnet.

One page, rendered on the server from the secretary's latest snapshot, refreshing itself.
Every reading shows when it was taken and which agent reported it. No external assets.

    GET /                 the page
    GET /state.json       the snapshot, for scripts
    GET /logs/{agent}     an agent's event log (admins only: recognised by `tailscale whois`)
"""
import datetime as dt
import html

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
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
.loglink{font-weight:400;color:var(--meter);text-decoration:none;margin-left:6px}
.loglink:hover{text-decoration:underline}
.ahead{display:flex;justify-content:space-between;align-items:center;gap:8px}
.ahead h3{margin-bottom:0}.check{margin:14px 0 0}.check button{font:12px system-ui,sans-serif;
padding:3px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);
color:var(--fg);cursor:pointer}.check button:hover{border-color:var(--meter)}
@media (max-width:420px){main{padding:12px;grid-template-columns:1fr}header{padding:14px 16px}}
"""

LIMIT_NAMES = {"five_hour": "5-hour window", "seven_day": "weekly", "seven_day_opus":
               "weekly (Opus)", "seven_day_sonnet": "weekly (Sonnet)", "overage": "overage"}
STATE_CLASS = {"done": "ok", "running": "", "queued": "muted", "awaiting_approval": "warn",
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
    cards = []
    for hid, acc in state["accounts"].items():
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
    return "<section><h2>Claude usage, by account</h2>" + "".join(cards) + "</section>"


def _workers(state, admin=False):
    out = []
    for aid, a in state["agents"].items():
        if a.get("role") != "worker":
            continue
        if not a.get("up"):
            out.append(f"<h2>{e(a['name'])} <span class='pill bad'>down</span></h2>"
                       f"<p class='muted small'>{e(a.get('error'))}</p>")
            continue
        q = a.get("queue") or {}
        busy = q.get("running", 0) + q.get("awaiting_approval", 0)
        status = ("<span class='pill warn'>busy</span>" if busy else
                  "<span class='pill ok'>idle</span>")
        rows = "".join(
            f"<tr><td><code>{e(t['id'])}</code><div class='muted small'>"
            f"{e((t.get('instruction') or '')[:90])}</div></td>"
            f"<td class='{STATE_CLASS.get(t['state'], '')}'>{e(t['state'])}</td>"
            f"<td>{e(t.get('requester'))}<div class='muted small'>via {e(t.get('manager'))}</div></td>"
            f"<td class='muted small'>{e(ago(t.get('finished') or t.get('created')))}</td></tr>"
            for t in a.get("recent") or [])
        out.append(f"<h2>{e(a['name'])} {status}{_log_link(aid) if admin else ''}</h2>"
                   f"<div class='small'>GPU: {e(a.get('gpu'))} · disk free "
                   f"{e(a.get('disk_free_gb'))} GB</div>"
                   f"<table><tr><th>Task</th><th>State</th><th>For</th><th>When</th></tr>"
                   f"{rows or '<tr><td class=muted>no tasks yet</td></tr>'}</table>")
    return "<section>" + ("".join(out) or "<h2>Workers</h2><p class=muted>none</p>") + "</section>"


def _approvals(state):
    ap = state.get("approvals") or {}
    if ap.get("error"):
        return f"<section><h2>Approvals</h2><p class='muted'>{e(ap['error'])}</p></section>"
    pend = "".join(f"<tr><td>{e(r['agent'])}</td><td><code>{e(r['detail'][:120])}</code></td>"
                   f"<td>{e(r['human'])}</td><td class='muted small'>until "
                   f"{e(r['expires'][11:16])} UTC</td></tr>" for r in ap.get("pending") or [])
    grants = "".join(f"<tr><td>{e(agent)}</td><td>{e(g['kind'])} <b>{e(g['value'])}</b></td>"
                     f"<td>{e(g.get('granted_by'))}</td><td class='muted small'>until "
                     f"{e(g['expires'][11:16])} UTC</td></tr>"
                     for agent, gs in (ap.get("grants") or {}).items() for g in gs)
    return ("<section><h2>Approvals</h2><table><tr><th>Agent</th><th>Asks</th><th>Who decides"
            "</th><th></th></tr>" + (pend or "<tr><td class=muted colspan=4>nothing pending"
                                     "</td></tr>") +
            "</table><h2 style='margin-top:14px'>Active grants</h2><table><tr><th>Agent</th>"
            "<th>Grant</th><th>Given by</th><th></th></tr>" +
            (grants or "<tr><td class=muted colspan=4>none</td></tr>") + "</table></section>")


def _hosts(state):
    hosts = state.get("hosts") or {}
    if not hosts:
        return ""
    cls = {"RUNNING": "ok", "STOPPED": "muted", "STOPPING": "warn", "STARTING": "warn"}
    rows = "".join(
        f"<tr><td><b>{e(h)}</b></td><td class='{cls.get(v.get('state'), 'muted')}'>"
        f"{e((v.get('state') or '—').lower())}</td><td class='small'>{e(v.get('power') or '')}"
        + (f"<div class='muted small'>idle since {e(ago(v['idle_since']))}</div>"
           if v.get("idle_since") else "")
        + f"</td><td class='muted small'>{e(ago(v.get('at')) if v.get('at') else '')}</td></tr>"
        for h, v in hosts.items())
    return ("<section><h2>Machines</h2><table><tr><th>Host</th><th>Power</th><th>Policy</th>"
            "<th>Checked</th></tr>" + rows + "</table></section>")


def _log_link(aid):
    return f" <a class='loglink small' href='/logs/{e(aid)}'>log →</a>"


def _agents(state, admin=False):
    rows = "".join(
        f"<tr><td><b>{e(a['name'])}</b><div class='muted small'>{e(aid)}</div></td>"
        f"<td>{e(a.get('role'))}</td><td>{e(a.get('human'))}</td><td>{e(a.get('host'))}</td>"
        f"<td class='{'ok' if a.get('service') == 'active' or a.get('up') else 'muted'}'>"
        f"{e(a.get('service') or ('up' if a.get('up') else '—'))}</td>"
        + (f"<td>{_log_link(aid)}</td>" if admin else "") + "</tr>"
        for aid, a in state["agents"].items())
    return ("<section><h2>Agents</h2><table><tr><th>Agent</th><th>Role</th><th>Seat</th>"
            "<th>Host</th><th>Status</th>" + ("<th>Log</th>" if admin else "") + "</tr>"
            + rows + "</table>"
            + ("<p class='muted small'>Each agent's log: what it was asked, what it did and "
               "said, approvals. Admins only.</p>" if admin else "") + "</section>")


def render(state, admin=False):
    if state is None:
        body = "<main><section><p>Collecting the lab's state…</p></section></main>"
        title, when = "Agentic Lab", ""
    else:
        title = f"{state['lab']} · Agentic Lab"
        when = f"updated {ago(state['at'])}"
        body = ("<main>" + _accounts(state) + _workers(state, admin) + _approvals(state) +
                _hosts(state) + _agents(state, admin) + "</main>")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{e(title)}</title>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<meta http-equiv='refresh' content='{REFRESH_S}'><style>{CSS}</style></head>"
            f"<body><header><h1>{e(title)}</h1><span class='muted small'>{e(when)} · "
            f"monitoring only; approvals happen in Slack</span></header>{body}</body></html>")


KIND_CLASS = {"message": "", "reply": "ok", "tool": "muted", "result": "muted",
              "approval": "warn", "task": "", "error": "bad"}
HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
           "Cache-Control": "no-store"}


def render_logs(state, agent, hours, events, error=None):
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
            f"<span class='muted small'>admins only</span></header>{body}</body></html>")


def app(get_state, get_events=None, is_admin=None, check_usage=None):
    """`get_events(agent, hours)` (async) -> events; `is_admin(client_ip)` (async) -> bool;
    `check_usage(human)` (async): read that seat's limits now."""
    async def check(request):
        if check_usage is not None:
            await check_usage(request.path_params["human"])
        return RedirectResponse("/", status_code=303)

    async def page(request):
        admin = is_admin is not None and await is_admin(request.client.host)
        return HTMLResponse(render(get_state(), admin=admin), headers=HEADERS)

    async def state_json(request):
        return JSONResponse(get_state() or {})

    async def logs(request):
        agent = request.path_params["agent"]
        if is_admin is None or get_events is None or not await is_admin(request.client.host):
            return HTMLResponse("<!doctype html><p>Agents' logs are for the lab's admins only.</p>",
                                status_code=403, headers=HEADERS)
        try:
            hours = min(max(float(request.query_params.get("hours", 24)), 0.1), 336)
        except ValueError:
            hours = 24
        try:
            events, error = await get_events(agent, hours), None
        except Exception as ex:
            events, error = [], f"couldn't fetch the log: {ex}"
        return HTMLResponse(render_logs(get_state(), agent, hours, events, error), headers=HEADERS)

    return Starlette(routes=[Route("/", page), Route("/state.json", state_json),
                             Route("/logs/{agent}", logs),
                             Route("/usage/check/{human}", check, methods=["POST"])])
