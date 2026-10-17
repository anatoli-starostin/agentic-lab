"""The lab's web UI (DESIGN.md §9): monitoring only, on the tailnet.

One page, rendered on the server from the secretary's latest snapshot, refreshing itself.
Every reading shows when it was taken and which agent reported it. No external assets.

    GET /            the page
    GET /state.json  the snapshot, for scripts
"""
import datetime as dt
import html

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

REFRESH_S = 20
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#6b7280;--line:#e3e6eb;--ok:#1f8a4c;
--warn:#b7791f;--bad:#c53030;--bar:#3b82f6;--barbg:#e5e7eb}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1a1f27;--fg:#e6e9ef;
--muted:#9aa3b2;--line:#2a313c;--ok:#48bb78;--warn:#ecc94b;--bad:#fc8181;--bar:#60a5fa;
--barbg:#2a313c}}
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
.pill{display:inline-block;padding:1px 8px;border-radius:999px;border:1px solid var(--line);
font-size:12px}code{font-size:12px;word-break:break-all}
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


def _accounts(state):
    rows = []
    for hid, acc in state["accounts"].items():
        agents = ", ".join(e(state["agents"][a]["name"]) for a in acc["agents"]) or "—"
        lims = []
        for kind, lim in sorted(acc["limits"].items()):
            pct = round(100 * (lim.get("utilization") or 0))
            cls = "bad" if lim.get("status") == "rejected" else (
                "warn" if lim.get("status") == "allowed_warning" else "")
            resets = (lim.get("resets_at") or "")[11:16]
            lims.append(
                f'<div>{e(LIMIT_NAMES.get(kind, kind))} <b class="{cls}">{pct}%</b>'
                f'{f" · resets {resets} UTC" if resets else ""}'
                f'<div class="bar"><i style="width:{min(pct, 100)}%"></i></div>'
                f'<span class="muted small">as of {e(ago(lim.get("observed_at")))}, '
                f'from {e(lim.get("from"))}</span></div>')
        if not lims:
            lims.append('<span class="muted small">no limit readings yet (they arrive while '
                        'an agent on this seat runs)</span>')
        tok = " · ".join(f"{e(a)} {n:,}" for a, n in acc["tokens_today"].items()) or "—"
        rows.append(f"<tr><td><b>{e(acc['name'])}</b><div class='muted small'>{agents}</div>"
                    f"</td><td>{''.join(lims)}<div class='small'>tokens today: {tok}</div>"
                    f"</td></tr>")
    return ("<section><h2>Claude usage, by account</h2><table><tr><th>Account</th>"
            "<th>Limits &amp; tokens</th></tr>" + "".join(rows) + "</table></section>")


def _workers(state):
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
        out.append(f"<h2>{e(a['name'])} {status}</h2>"
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


def _agents(state):
    rows = "".join(
        f"<tr><td><b>{e(a['name'])}</b><div class='muted small'>{e(aid)}</div></td>"
        f"<td>{e(a.get('role'))}</td><td>{e(a.get('human'))}</td><td>{e(a.get('host'))}</td>"
        f"<td class='{'ok' if a.get('service') == 'active' or a.get('up') else 'muted'}'>"
        f"{e(a.get('service') or ('up' if a.get('up') else '—'))}</td></tr>"
        for aid, a in state["agents"].items())
    return ("<section><h2>Agents</h2><table><tr><th>Agent</th><th>Role</th><th>Seat</th>"
            "<th>Host</th><th>Status</th></tr>" + rows + "</table></section>")


def render(state):
    if state is None:
        body = "<main><section><p>Collecting the lab's state…</p></section></main>"
        title, when = "Agentic Lab", ""
    else:
        title = f"{state['lab']} · Agentic Lab"
        when = f"updated {ago(state['at'])}"
        body = ("<main>" + _accounts(state) + _workers(state) + _approvals(state) +
                _agents(state) + "</main>")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{e(title)}</title>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<meta http-equiv='refresh' content='{REFRESH_S}'><style>{CSS}</style></head>"
            f"<body><header><h1>{e(title)}</h1><span class='muted small'>{e(when)} · "
            f"monitoring only; approvals happen in Slack</span></header>{body}</body></html>")


def app(get_state):
    async def page(request):
        return HTMLResponse(render(get_state()),
                            headers={"Content-Security-Policy": "default-src 'none'; "
                                     "style-src 'unsafe-inline'", "Cache-Control": "no-store"})

    async def state_json(request):
        return JSONResponse(get_state() or {})

    return Starlette(routes=[Route("/", page), Route("/state.json", state_json)])
