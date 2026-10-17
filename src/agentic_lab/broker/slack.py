"""The Slack DM permission channel (DESIGN.md §7).

The broker's own Slack app ("Lab Approvals") DMs the human each request, with buttons:
Run once · <grant> for 1 hour · <grant> for this task · Deny. A reply in the request's
thread denies it with that text as a note for the agent. When a request is decided (or
times out), its message is updated to say so, and the buttons go away.

Socket Mode: no public URL; events arrive over a connection authenticated with the app
token, so the clicking user's id in a payload comes from Slack itself. The broker checks
it is the person the request is for.
"""
import asyncio
import datetime as dt
import logging

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from .core import PENDING, Broker, BrokerError, Request

log = logging.getLogger("agentic_lab.broker.slack")

LABELS = {"once": "✅ allowed once", "hour": "✅ granted for 1 hour",
          "task": "✅ granted for this task", "deny": "⛔ denied", "timeout": "⌛ timed out (denied)"}


def _grant_text(options):
    return ", ".join(f"{o['value']}" if o["kind"] == "domain" else
                     f"writes to {o['value']}" if o["kind"] == "write" else o["value"]
                     for o in options)


def request_blocks(req: Request):
    """The DM for a pending request."""
    until = dt.datetime.fromisoformat(req.expires).strftime("%H:%M UTC")
    task = f" (task `{req.task_id}`)" if req.task_id else ""
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f"*{req.agent}* asks to do something outside its sandbox{task}:"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{req.detail[:2800]}```"}},
    ]
    if req.violations:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                       "text": "Sandbox refused: " + "; ".join(req.violations)[:2000]}]})
    if req.context:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"_Note from the manager:_ {req.context}"}})
    buttons = [{"type": "button", "style": "primary", "action_id": "once",
                "text": {"type": "plain_text", "text": "Run once"}, "value": f"{req.id}|once"}]
    if req.options:
        what = _grant_text(req.options)[:40]
        buttons += [
            {"type": "button", "action_id": "hour", "value": f"{req.id}|hour",
             "text": {"type": "plain_text", "text": f"Allow {what} · 1 hour"[:75]}},
            {"type": "button", "action_id": "task", "value": f"{req.id}|task",
             "text": {"type": "plain_text", "text": f"Allow {what} · this task"[:75]}},
        ]
    buttons.append({"type": "button", "style": "danger", "action_id": "deny",
                    "text": {"type": "plain_text", "text": "Deny"}, "value": f"{req.id}|deny"})
    blocks += [
        {"type": "actions", "block_id": "decide", "elements": buttons},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
         f"Reply in this thread to deny with a note for the agent · auto-deny at {until}"}]},
    ]
    return blocks


def decided_blocks(req: Request):
    d = req.decision or {}
    action = d.get("action")
    line = LABELS.get(action, action)
    if action in ("hour", "task") and d.get("grants"):
        line += f": {_grant_text(d['grants'])}"
    if d.get("note"):
        line += f'\n> {d["note"][:500]}'
    by = f" by {d['by']}" if d.get("by") else ""
    return [
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f"*{req.agent}*: ```{req.detail[:1500]}```"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{line}{by}"}]},
    ]


class SlackChannel:
    def __init__(self, broker: Broker, bot_token, app_token):
        self.broker = broker
        self.web = AsyncWebClient(token=bot_token)
        self.socket = SocketModeClient(app_token=app_token, web_client=self.web)
        self.loop = None
        broker.listeners.append(self._on_change)

    # broker -> Slack
    def _on_change(self, req: Request):
        if self.loop is None:
            return
        coro = self._post(req) if req.state == PENDING and not req.channel_ref else self._update(req)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self.loop.create_task(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _post(self, req: Request):
        user = self.broker.humans.get(req.human, {}).get("slack_user")
        if not user:
            log.error("no slack_user for %s; can't ask about %s", req.human, req.id)
            return
        try:
            im = await self.web.conversations_open(users=user)
            channel = im["channel"]["id"]
            r = await self.web.chat_postMessage(channel=channel, blocks=request_blocks(req),
                                                text=f"{req.agent} asks for approval: {req.detail[:200]}")
            self.broker.set_channel_ref(req.id, {"channel": channel, "ts": r["ts"]})
        except Exception:
            log.exception("could not DM %s about %s", req.human, req.id)

    async def _update(self, req: Request):
        ref = req.channel_ref or self.broker.get(req.id).channel_ref
        if not ref:
            return
        blocks = decided_blocks(req) if req.state != PENDING else request_blocks(req)
        try:
            await self.web.chat_update(channel=ref["channel"], ts=ref["ts"], blocks=blocks,
                                       text=f"{req.agent}: {req.detail[:200]}")
        except Exception:
            log.exception("could not update the message for %s", req.id)

    # Slack -> broker
    async def _on_socket(self, client: SocketModeClient, sreq: SocketModeRequest):
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=sreq.envelope_id))
        p = sreq.payload or {}
        try:
            if sreq.type == "interactive" and p.get("type") == "block_actions":
                user = (p.get("user") or {}).get("id")
                for a in p.get("actions") or []:
                    rid, _, action = (a.get("value") or "").partition("|")
                    self._decide(rid, action, user, None, p)
            elif sreq.type == "events_api":
                ev = p.get("event") or {}
                if (ev.get("type") == "message" and ev.get("channel_type") == "im"
                        and ev.get("thread_ts") and not ev.get("bot_id")
                        and not ev.get("subtype")):
                    req = self._by_thread(ev["channel"], ev["thread_ts"])
                    if req:
                        self._decide(req.id, "deny", ev.get("user"), ev.get("text", ""), None)
        except Exception:
            log.exception("handling a Slack event failed")

    def _by_thread(self, channel, ts):
        for req in self.broker.pending():
            if req.channel_ref == {"channel": channel, "ts": ts}:
                return req
        return None

    def _decide(self, rid, action, user, note, payload):
        try:
            self.broker.decide(rid, action, user, note=note)
        except BrokerError as e:
            log.warning("decision on %s by %s refused: %s", rid, user, e)
            if payload and payload.get("channel"):
                self.loop.create_task(self.web.chat_postEphemeral(
                    channel=payload["channel"]["id"], user=user, text=f"Not applied: {e}"))

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.socket.socket_mode_request_listeners.append(self._on_socket)
        await self.socket.connect()
        # requests filed while we were down: post them now
        for req in self.broker.pending():
            if not req.channel_ref:
                await self._post(req)

    async def stop(self):
        await self.socket.close()
