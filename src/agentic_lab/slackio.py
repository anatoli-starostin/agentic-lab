"""An agent's presence in Slack: what it sees, and how it answers (DESIGN.md §4.3).

Shared by managers and the secretary. Scope:
  - A DM is one conversation (its threads are conversations of their own).
  - In channels, the agent takes part only in threads where it's mentioned or already
    involved, never the whole channel. When first mentioned partway through a thread, it
    reads the thread so far.
  - In a thread it's involved in, it sees every message and decides for itself whether to
    answer (it may answer `<silent>`): judgment stays in the model, not in filters.
  - Every message is checked against lab.yaml: the sender must be a lab member who may use
    this agent (a dedicated agent serves only its own member).
  - Attachments are downloaded into the agent's working folder; the agent is told where.

Socket Mode: no public URL, events arrive over a connection authenticated with the app token.
"""
import asyncio
import logging
import os
import re

import aiohttp
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

log = logging.getLogger("agentic_lab.slack")

SILENT = "<silent>"
MAX_TEXT = 3800                 # Slack's limit is ~4000 characters per message
MAX_FILE_BYTES = 50 * 1024 * 1024
HISTORY_MESSAGES = 50           # how much of a thread is read on first mention


class Incoming:
    """One message the agent should consider."""

    def __init__(self, channel, thread_ts, ts, user, human, text, private, mentioned,
                 history=None, files=None):
        self.channel, self.thread_ts, self.ts = channel, thread_ts, ts
        self.user, self.human, self.text = user, human, text
        self.private = private          # a DM
        self.mentioned = mentioned      # addressed to this agent (DM, or @mention)
        self.history = history or []    # earlier thread messages, on first involvement
        self.files = files or []        # local paths of attachments


class SlackIO:
    def __init__(self, bot_token, app_token, members, may_use, on_message, inbox,
                 engaged=None, on_home=None, on_action=None):
        """`members`: {slack user id: human id}; `may_use(human) -> (ok, reason)`;
        `on_message(Incoming)` is awaited per message (the caller serializes per
        conversation); `inbox`: folder for attachments; `engaged`: a set of thread keys
        the agent is involved in (persisted by the caller)."""
        self.bot_token = bot_token
        self.web = AsyncWebClient(token=bot_token)
        self.socket = SocketModeClient(app_token=app_token, web_client=self.web)
        self.members, self.may_use, self.on_message = members, may_use, on_message
        self.inbox = inbox
        self.engaged = engaged if engaged is not None else set()
        self.on_home = on_home          # awaited with (slack user id) when the Home tab opens
        # awaited with (slack user, lab member or None, action_id, value, {channel, ts,
        # thread_ts}) when someone clicks one of the agent's buttons
        self.on_action = on_action
        self.me = None

    # ── sending ──────────────────────────────────────────────────────────────

    async def post(self, channel, thread_ts, text):
        if not text or text.strip() == SILENT:
            return
        for i in range(0, len(text), MAX_TEXT):
            await self.web.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                            text=text[i:i + MAX_TEXT], unfurl_links=False)

    async def upload(self, channel, thread_ts, path, title, comment=None):
        await self.web.files_upload_v2(channel=channel, thread_ts=thread_ts, file=path,
                                       title=title, initial_comment=comment or None)

    async def react(self, channel, ts, name, add=True):
        try:
            if add:
                await self.web.reactions_add(channel=channel, timestamp=ts, name=name)
            else:
                await self.web.reactions_remove(channel=channel, timestamp=ts, name=name)
        except Exception:
            pass                        # a missing reaction is never worth failing over

    # ── receiving ────────────────────────────────────────────────────────────

    def _strip_mention(self, text):
        return re.sub(rf"<@{self.me}>\s*", "", text or "").strip()

    async def _download(self, files, key):
        out = []
        folder = os.path.join(self.inbox, re.sub(r"[^A-Za-z0-9._-]", "_", key))
        os.makedirs(folder, exist_ok=True)
        async with aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.bot_token}"}) as http:
            for f in files:
                url = f.get("url_private_download") or f.get("url_private")
                if not url or (f.get("size") or 0) > MAX_FILE_BYTES:
                    continue
                name = os.path.basename(f.get("name") or f.get("id") or "file")
                path = os.path.join(folder, name)
                async with http.get(url) as r:
                    if r.status != 200:
                        continue
                    with open(path, "wb") as fh:
                        fh.write(await r.read())
                out.append(path)
        return out

    async def _history(self, channel, thread_ts, before_ts):
        try:
            r = await self.web.conversations_replies(channel=channel, ts=thread_ts,
                                                     limit=HISTORY_MESSAGES)
        except Exception:
            return []
        lines = []
        for m in r.get("messages", []):
            if m.get("ts") == before_ts:
                break
            who = self.members.get(m.get("user"), m.get("username") or m.get("user") or "bot")
            if m.get("bot_id") and m.get("user") == self.me:
                who = "you"
            lines.append(f"{who}: {m.get('text', '')}")
        return lines

    async def post_blocks(self, channel, thread_ts, text, blocks):
        r = await self.web.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text,
                                            blocks=blocks, unfurl_links=False)
        return r.get("ts")

    async def _on_socket(self, client: SocketModeClient, req: SocketModeRequest):
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type == "interactive":
            p = req.payload or {}
            if p.get("type") != "block_actions" or self.on_action is None:
                return
            user = (p.get("user") or {}).get("id")
            msg = p.get("message") or {}
            where = {"channel": (p.get("channel") or {}).get("id"), "ts": msg.get("ts"),
                     "thread_ts": msg.get("thread_ts")}
            for a in p.get("actions") or []:
                try:            # the clicker is who Slack says it is: checked by the handler
                    await self.on_action(user, self.members.get(user), a.get("action_id"),
                                         a.get("value"), where)
                except Exception:
                    log.exception("handling a button failed")
            return
        if req.type != "events_api":
            return
        ev = (req.payload or {}).get("event") or {}
        try:
            await self._on_event(ev)
        except Exception:
            log.exception("handling a Slack event failed")

    async def _on_event(self, ev):
        etype, user = ev.get("type"), ev.get("user")
        if etype == "app_home_opened":
            if self.on_home is not None and ev.get("tab") == "home" and user in self.members:
                await self.on_home(user)
            return
        if ev.get("bot_id") or user == self.me or not user:
            return                      # never answer bots (including itself)
        if ev.get("subtype") not in (None, "file_share"):
            return                      # edits, joins, deletions...
        channel, ts = ev.get("channel"), ev.get("ts")
        private = ev.get("channel_type") == "im"
        mentioned_here = f"<@{self.me}>" in (ev.get("text") or "")

        if private:
            if etype != "message":
                return
            thread_ts, mentioned = ev.get("thread_ts"), True
        elif etype == "app_mention":
            thread_ts, mentioned = ev.get("thread_ts") or ts, True
        elif etype == "message" and ev.get("thread_ts"):
            thread_ts = ev["thread_ts"]
            if mentioned_here or f"{channel}/{thread_ts}" not in self.engaged:
                return                  # mentions arrive as app_mention; other threads aren't ours
            mentioned = False
        else:
            return

        human = self.members.get(user)
        if human is None:
            if mentioned:
                await self.post(channel, thread_ts if not private else None,
                                "Sorry, I only work with members of this lab.")
            return
        ok, reason = self.may_use(human)
        if not ok:
            if mentioned:
                await self.post(channel, thread_ts if not private else None, reason)
            return

        history = []
        key = channel if private and thread_ts is None else f"{channel}/{thread_ts}"
        if not private and key not in self.engaged:
            self.engaged.add(key)
            if thread_ts != ts:         # mentioned partway through a thread: read it first
                history = await self._history(channel, thread_ts, ts)
        files = await self._download(ev.get("files") or [], key) if ev.get("files") else []
        await self.on_message(Incoming(channel, thread_ts, ts, user, human,
                                       self._strip_mention(ev.get("text")), private,
                                       mentioned, history, files))

    async def start(self):
        auth = await self.web.auth_test()
        self.me = auth["user_id"]
        self.socket.socket_mode_request_listeners.append(self._on_socket)
        await self.socket.connect()

    async def stop(self):
        await self.socket.close()
