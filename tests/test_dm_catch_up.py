"""#70: a manager catches up on the DMs it missed while it wasn't running."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from agentic_lab import registry, slackio
from agentic_lab.manager import service as S
from agentic_lab.manager.state import ManagerState
from agentic_lab.slackio import SlackIO

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal" / "lab.yaml"


class History:
    """conversations.history / .replies: messages in [oldest, latest), like Slack (history
    newest first, replies oldest first with the thread's parent always included)."""
    def __init__(self, msgs, fail=False):
        self.msgs, self.fail, self.calls = msgs, fail, []

    def _between(self, kw):
        return [m for m in self.msgs
                if float(kw["oldest"]) < float(m["ts"]) < float(kw["latest"])]

    async def conversations_history(self, cursor=None, **kw):
        self.calls.append(("history", kw))
        if self.fail:
            raise RuntimeError("slack down")
        return {"messages": sorted(self._between(kw), key=lambda m: -float(m["ts"]))}

    async def conversations_replies(self, cursor=None, **kw):
        self.calls.append(("replies", kw))
        parent = [m for m in self.msgs if m["ts"] == kw["ts"]]
        return {"messages": parent + [m for m in self._between(kw) if m not in parent]}


def make_io(msgs, fail=False):
    io = SlackIO.__new__(SlackIO)
    io.web, io.members, io.me = History(msgs, fail), {"U-ALICE": "alice"}, "U-BOT"
    return io


def dm(ts, text, user="U-ALICE", **kw):
    return {"ts": ts, "user": user, "text": text, **kw}


NOW = 1_800_000_000.0


def test_missed_dms_are_the_members_messages_in_between_oldest_first():
    io = make_io([dm(f"{NOW - 50:.6f}", "seen already"),
                  dm(f"{NOW - 40:.6f}", "first missed"),
                  dm(f"{NOW - 30:.6f}", "a reply", user="U-BOT", bot_id="B1"),
                  dm(f"{NOW - 20:.6f}", "", files=[{"id": "F1"}], subtype="file_share"),
                  dm(f"{NOW - 15:.6f}", "edited", subtype="message_changed"),
                  dm(f"{NOW - 10:.6f}", "second missed"),
                  dm(f"{NOW:.6f}", "the new one")])
    lines = asyncio.run(io.missed_dms("D1", None, f"{NOW - 50:.6f}", f"{NOW:.6f}"))
    assert lines == ["alice: first missed", "alice: [attachments, not downloaded]",
                     "alice: second missed"]
    kind, kw = io.web.calls[0]
    assert kind == "history" and kw["channel"] == "D1" and kw["inclusive"] is False


def test_missed_dms_are_bounded():
    many = [dm(f"{NOW - 100 + i:.6f}", f"m{i} " + "x" * 2000) for i in range(25)]
    io = make_io(many)
    lines = asyncio.run(io.missed_dms("D1", None, f"{NOW - 200:.6f}", f"{NOW:.6f}"))
    assert len(lines) == slackio.DM_CATCHUP_MAX + 1
    assert lines[0] == "[... 5 earlier messages not shown ...]"
    assert lines[1].startswith("alice: m5 ") and len(lines[1]) == slackio.DM_CATCHUP_CHARS
    old = make_io([dm(f"{NOW - 4 * 86400:.6f}", "four days ago"),
                   dm(f"{NOW - 3600:.6f}", "an hour ago")])
    lines = asyncio.run(old.missed_dms("D1", None, f"{NOW - 5 * 86400:.6f}", f"{NOW:.6f}"))
    assert lines == ["alice: an hour ago"]                 # DM_CATCHUP_HOURS back at most
    assert float(old.web.calls[0][1]["oldest"]) == NOW - slackio.DM_CATCHUP_HOURS * 3600


def test_missed_dms_in_a_dm_thread_and_when_slack_fails():
    io = make_io([dm("100.000000", "the thread's start"), dm("150.000000", "missed"),
                  dm("200.000000", "new")])
    lines = asyncio.run(io.missed_dms("D1", "100.000000", "120.000000", "200.000000"))
    assert lines == ["alice: missed"] and io.web.calls[0][0] == "replies"
    assert io.web.calls[0][1]["ts"] == "100.000000"
    down = make_io([dm("150.000000", "missed")], fail=True)
    assert asyncio.run(down.missed_dms("D1", None, "120.000000", "200.000000")) == []


class Slack:
    def __init__(self, missed=("alice: are you there?",)):
        self.missed, self.calls, self.posts = list(missed), [], []

    async def react(self, *a, **k):
        pass

    async def post(self, channel, thread, text):
        self.posts.append(text)

    async def missed_dms(self, channel, thread_ts, after_ts, before_ts):
        self.calls.append((channel, thread_ts, after_ts, before_ts))
        return self.missed


class Brain:
    def __init__(self):
        self.prompts = []

    async def turn(self, ctx, prompt):
        self.prompts.append(prompt)
        return "ok"


def manager(tmp_path, slack=None):
    lab = registry.load(EXAMPLE)
    agent = next(a for a, v in lab.agents.items() if v.get("role") == "manager")
    owner = lab.agents[agent]["human"]
    m = S.Manager(lab, agent, slack or Slack(), Brain(), None, None,
                  ManagerState(str(tmp_path)), None, str(tmp_path))
    return m, owner


def msg(human, ts, text, channel="D1", thread_ts=None, private=True):
    return SimpleNamespace(channel=channel, thread_ts=thread_ts, ts=ts, human=human,
                           text=text, private=private, mentioned=True, history=[], files=[])


def test_the_first_dm_after_a_restart_catches_up_once(tmp_path):
    m, owner = manager(tmp_path)
    asyncio.run(m.on_message(msg(owner, "100.000000", "hi")))
    assert m.slack.calls == [] and not m.brain.prompts[0].startswith("[lab]")  # never seen
    assert m.state.conversations["D1"].last_seen_ts == "100.000000"
    asyncio.run(m.on_message(msg(owner, "110.000000", "and?")))
    assert m.slack.calls == []                             # running all along: nothing missed

    m2, _ = manager(tmp_path)                              # restarted
    asyncio.run(m2.on_message(msg(owner, "300.000000", "back?")))
    assert m2.slack.calls == [("D1", None, "110.000000", "300.000000")]
    p = m2.brain.prompts[0]
    assert p.startswith(S.CATCH_UP + "alice: are you there?\n\n") and p.endswith("back?")
    assert m2.state.conversations["D1"].last_seen_ts == "300.000000"
    asyncio.run(m2.on_message(msg(owner, "310.000000", "still?")))
    assert len(m2.slack.calls) == 1 and not m2.brain.prompts[1].startswith("[lab]")
    with open(tmp_path / "manager-state.json") as f:
        assert json.load(f)["conversations"]["D1"]["last_seen_ts"] == "310.000000"


def test_nothing_missed_means_no_note_and_threads_are_untouched(tmp_path):
    m, owner = manager(tmp_path, Slack(missed=[]))
    m.state.conversation("D1", None).last_seen_ts = "100.000000"
    asyncio.run(m.on_message(msg(owner, "200.000000", "hi")))
    assert m.slack.calls and m.brain.prompts[0].endswith("hi")
    assert "[lab]" not in m.brain.prompts[0]
    asyncio.run(m.on_message(msg(owner, "300.000000", "<@U-BOT> go", channel="C1",
                                 thread_ts="250.000000", private=False)))
    assert len(m.slack.calls) == 1                         # a channel thread: no DM catch-up
    assert m.state.conversations["C1/250.000000"].last_seen_ts is None


def test_a_dm_thread_catches_up_on_its_own(tmp_path):
    m, owner = manager(tmp_path)
    m.state.conversation("D1", "50.000000").last_seen_ts = "60.000000"
    asyncio.run(m.on_message(msg(owner, "90.000000", "more", thread_ts="50.000000")))
    assert m.slack.calls == [("D1", "50.000000", "60.000000", "90.000000")]


def test_a_state_file_from_before_the_catch_up_loads(tmp_path):
    with open(tmp_path / "manager-state.json", "w") as f:
        json.dump({"conversations": {"D1": {"key": "D1", "channel": "D1", "thread_ts": None,
                                            "session_id": "s-1"}}}, f)
    st = ManagerState(str(tmp_path))
    assert st.conversations["D1"].last_seen_ts is None and st.conversations["D1"].session_id
