"""The secretary's Home tab in Slack: the lab at a glance, for the person looking.

Slack's Home tab isn't a web page: the app publishes a view made of Slack's own blocks
(`views.publish`). So this is a Slack-native digest of the web UI, personalised (their
account's usage and their tasks first), with a button to the full dashboard.
"""
import datetime as dt

from .web import LIMIT_NAMES, STATUS, WINDOWS, ago, human, resets_in

BAR = 10                    # characters in a text meter
MAX_TASKS = 4


def text_meter(fraction):
    full = round(max(0.0, min(1.0, fraction)) * BAR)
    return "▰" * full + "▱" * (BAR - full)


def _section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}


def _context(text):
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:2900]}]}


def _usage(state, human_id):
    acc = (state.get("accounts") or {}).get(human_id)
    if not acc:
        return [_section("_No agents run on your Claude seat._")]
    lines = []
    order = [k for k, _, _ in WINDOWS]
    for kind in sorted(acc["limits"], key=lambda k: order.index(k) if k in order else 99):
        lim = acc["limits"][kind]
        title = next((f"{t} ({s})" for k, t, s in WINDOWS if k == kind), LIMIT_NAMES.get(kind, kind))
        icon, word, _ = STATUS.get(lim.get("status"), ("", lim.get("status") or "", ""))
        left = resets_in(lim.get("resets_at"))
        if lim.get("utilization") is None:
            value = f"{icon} {word}"
        else:
            pct = round(100 * lim["utilization"])
            value = f"`{text_meter(lim['utilization'])}` *{pct}%*" + (
                f" {icon} {word}" if lim.get("status") != "allowed" else "")
        lines.append(f"*{title}*  {value}" + (f"  ·  resets in {left}" if left else "")
                     + f"\n_as of {ago(lim.get('observed_at'))}, from {lim.get('from')}_")
    if not lines:
        lines.append("_No limit readings yet: they arrive while one of your agents runs._")
    tokens = acc.get("tokens_today") or {}
    total = sum((v.get("new", 0) + v.get("cached", 0)) if isinstance(v, dict) else v
                for v in tokens.values())
    if total:
        parts = ", ".join(
            f"{state['agents'].get(a, {}).get('name', a)} {human((v.get('new', 0) + v.get('cached', 0)) if isinstance(v, dict) else v)}"
            for a, v in sorted(tokens.items(), key=lambda x: -((x[1].get('new', 0) + x[1].get('cached', 0)) if isinstance(x[1], dict) else x[1])))
        lines.append(f"*Tokens today*  {human(total)}  ({parts})")
    return [_section("\n\n".join(lines))]


def _workers(state, human_id):
    blocks = []
    for aid, a in (state.get("agents") or {}).items():
        if a.get("role") != "worker":
            continue
        if not a.get("up"):
            blocks.append(_section(f"🔴 *{a['name']}*  down"))
            continue
        q = a.get("queue") or {}
        busy = q.get("running", 0) + q.get("awaiting_approval", 0) + q.get("waiting", 0)
        head = f"{'🟡' if busy else '🟢'} *{a['name']}*  {'busy' if busy else 'idle'}"
        if q.get("queued"):
            head += f"  ·  {q['queued']} queued"
        if a.get("runs"):
            head += f"  ·  {len(a['runs'])} long run(s)"
        recent = sorted(a.get("recent") or [], key=lambda t: t.get("requester") != human_id)
        rows = [f"`{t['state']}`  {(t.get('instruction') or '')[:70]}"
                f"  _({t.get('requester')}, {ago(t.get('finished') or t.get('created'))})_"
                for t in recent[:MAX_TASKS]]
        blocks.append(_section(head + ("\n" + "\n".join(rows) if rows else "")))
    return blocks or [_section("_No workers._")]


def _approvals(state, human_id):
    ap = state.get("approvals") or {}
    mine = [r for r in ap.get("pending") or [] if r.get("human") == human_id]
    if not mine:
        return [_context("No approvals waiting for you.")]
    rows = [f"• *{r['agent']}*: `{r['detail'][:80]}`"
            + (f" (until {r['expires'][11:16]} UTC)" if r.get("expires") else "")
            for r in mine]
    return [_section(f"*⏳ {len(mine)} waiting for you* (in your DM from the approvals app)\n"
                     + "\n".join(rows))]


def _machines(state):
    hosts = state.get("hosts") or {}
    rows = [f"{'🟢' if (v.get('state') == 'RUNNING') else '⚪'} {h}  {(v.get('state') or '—').lower()}"
            + ("  ·  ⚠ stale (missed a deploy)" if v.get("stale") else "")
            for h, v in hosts.items() if v.get("state")]
    return [_section("*Machines*\n" + "\n".join(rows))] if rows else []


def view(state, human_id, dashboard_url, admin=False):
    """The Home tab for one person."""
    if not state:
        return {"type": "home", "blocks": [_section("Collecting the lab's state…")]}
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{state['lab']} · the lab"}},
        _context(f"updated {ago(state.get('at'))}"),
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Your Claude usage*"}},
        *_usage(state, human_id),
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Workers*"}},
        *_workers(state, human_id),
        *_approvals(state, human_id),
        *([{"type": "divider"}] + _machines(state) if _machines(state) else []),
        {"type": "divider"},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Open the full dashboard"},
             "url": dashboard_url, "action_id": "open_dashboard"}]},
        _context("The dashboard opens on the lab's tailnet." +
                 (" As an admin you can open each agent's log there." if admin else "")),
    ]
    return {"type": "home", "blocks": blocks[:100]}
