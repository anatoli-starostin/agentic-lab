"""A manager reads the channel of its conversation beyond its thread (read_channel), and
gets the channel's latest messages when mentioned at its top level."""
import asyncio
from types import SimpleNamespace

from agentic_lab.manager import tools as MT
from agentic_lab.manager.service import compose
from agentic_lab import registry
from agentic_lab.slackio import SlackIO

from test_lifecycle import manager_tools
from test_manager import EXAMPLE

NOW = 1794009380.0


class Web:
    def __init__(self, history=(), replies=()):
        self.history, self.replies, self.calls = list(history), list(replies), []

    async def _page(self, msgs, limit, cursor):
        start = int(cursor or 0)
        more = start + limit < len(msgs)
        return {"messages": msgs[start:start + limit],
                "response_metadata": {"next_cursor": str(start + limit) if more else ""}}

    async def conversations_history(self, channel, limit=200, cursor=None, oldest=None,
                                    latest=None, inclusive=None):
        self.calls.append(("history", channel, oldest, latest))
        msgs = [m for m in self.history if (oldest is None or float(m["ts"]) > float(oldest))
                and (latest is None or float(m["ts"]) < float(latest))]
        return await self._page(sorted(msgs, key=lambda m: -float(m["ts"])), limit, cursor)

    async def conversations_replies(self, channel, ts, limit=200, cursor=None, **kw):
        self.calls.append(("replies", channel, ts))
        return await self._page(self.replies, limit, cursor)


def io(web):
    seen = []

    async def on_message(msg):
        seen.append(msg)
    s = SlackIO.__new__(SlackIO)
    s.web, s.members, s.may_use, s.on_message = web, {"U-EUG": "eugene", "U-ANA": "anatoly"}, \
        (lambda h: (True, "")), on_message
    s.engaged, s.me, s.inbox, s.bot_token = set(), "U-BOT", "/tmp/unused", "x"
    return s, seen


def msg(ts, user, text, **kw):
    return {"ts": f"{ts:.6f}", "user": user, "text": text, **kw}


def test_a_top_level_mention_brings_the_channels_latest_messages():
    web = Web(history=[
        msg(NOW - 2 * 86400, "U-EUG", "two days ago"),               # older than a day
        msg(NOW - 600, "U-EUG", "white paper attached",
            files=[{"name": "systolic_lac.tex", "size": 200000}]),
        msg(NOW - 60, "U-ANA", "see Eugene's message"),
        {"ts": f"{NOW - 30:.6f}", "user": "U-EUG", "subtype": "channel_join", "text": "joined"},
    ])
    s, seen = io(web)
    asyncio.run(s._on_event({"type": "app_mention", "channel": "C1", "user": "U-ANA",
                             "ts": f"{NOW:.6f}", "text": "<@U-BOT> do the tutorial"}))
    recent = seen[0].channel_recent
    assert len(recent) == 2 and "eugene: white paper attached" in recent[0]
    assert "[files: systolic_lac.tex (195 KB)]" in recent[0] and "anatoly" in recent[1]
    p = compose(registry.load(EXAMPLE), SimpleNamespace(
        human="alice", text="do it", history=[], files=[], mentioned=True,
        channel_recent=recent))
    assert "Recent messages in this channel" in p and "read_channel" in p
    # midway through a thread: the thread, not the channel
    web.calls.clear()
    asyncio.run(s._on_event({"type": "app_mention", "channel": "C1", "user": "U-ANA",
                             "ts": f"{NOW + 5:.6f}", "thread_ts": f"{NOW - 600:.6f}",
                             "text": "<@U-BOT> and here"}))
    assert [c[0] for c in web.calls] == ["replies"] and seen[1].channel_recent == []


def test_a_slack_failure_leaves_out_the_recent_messages():
    s, seen = io(SimpleNamespace())             # no conversations_history at all
    asyncio.run(s._on_event({"type": "app_mention", "channel": "C1", "user": "U-ANA",
                             "ts": f"{NOW:.6f}", "text": "<@U-BOT> hi"}))
    assert seen[0].channel_recent == []


def test_read_channel_lists_the_top_level_or_a_thread():
    web = Web(history=[msg(NOW - 3600 * i, "U-EUG", f"m{i}") for i in range(5, 0, -1)]
              + [msg(NOW - 100, "U-EUG", "a thread start", reply_count=3,
                     thread_ts=f"{NOW - 100:.6f}")],
              replies=[msg(NOW - 100, "U-EUG", "a thread start")]
              + [msg(NOW - 90 + i, "U-ANA", f"r{i}") for i in range(10)])
    s, _ = io(web)
    lines, paths = asyncio.run(s.read_channel("C1", None, hours=72, limit=3))
    assert len(lines) == 3 and "m2" in lines[0] and "m1" in lines[1] and paths == []
    assert f"[thread: 3 replies, thread {NOW - 100:.6f}]" in lines[2]
    lines, _ = asyncio.run(s.read_channel("C1", f"{NOW - 100:.6f}", limit=4))
    assert "a thread start" in lines[0] and [x.rsplit(": ", 1)[1] for x in lines[1:]] == [
        "r7", "r8", "r9"]                       # its start, then the latest


def test_the_tool_reads_only_this_conversations_channel(monkeypatch):
    calls = []

    async def reader(channel, thread, hours, limit, key):
        calls.append((channel, thread, hours, limit, key))
        return ["[11-07 19:05 UTC, ts 1.0] eugene: here's the paper"], ["/w/inbox/C1_1.0/p.tex"]
    conv = SimpleNamespace(key="C1/1.0", channel="C1", thread_ts="1.0")
    ctx = SimpleNamespace(private=False, conv=conv, slack=SimpleNamespace(read_channel=reader))
    tools = manager_tools(ctx, monkeypatch)
    out = asyncio.run(tools["read_channel"]({"hours": 100000, "download_files": True}))
    text = out["content"][0]["text"]
    assert "here's the paper" in text and "Downloaded: /w/inbox/C1_1.0/p.tex" in text
    assert calls == [("C1", None, MT.CHANNEL_MAX_HOURS, MT.CHANNEL_LIMIT, "C1/1.0")]
    bad = asyncio.run(tools["read_channel"]({"thread": "C2; rm -rf"}))
    assert bad.get("is_error") and "isn't a thread ts" in bad["content"][0]["text"]
    ctx.private = True                          # a DM: no channel to read
    dm = asyncio.run(tools["read_channel"]({}))
    assert dm.get("is_error") and "DM" in dm["content"][0]["text"] and len(calls) == 1
