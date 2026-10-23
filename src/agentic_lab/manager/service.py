"""`lab manager serve <agent>`: a manager as a system service (DESIGN.md §4.3).

Runs as the manager's own Unix user. Its secrets (readable by the service, never by the
agent):

    ~/.config/agentic-lab/slack.env            the manager's Slack app: SLACK_BOT_TOKEN, SLACK_APP_TOKEN
    ~/.config/agentic-lab/workers.yaml         {worker-id: token} to reach workers
    ~/.config/agentic-lab/broker.yaml          {url, token} for approvals
    ~/.config/agentic-lab/claude-oauth-token   the seat token
    ~/.config/agentic-lab/monitor-token        the secretary's token for /v1/status
"""
import asyncio
import datetime as dt
import logging
import os
import signal
from types import SimpleNamespace

import yaml

from .. import github as GH
from ..broker.service import _env_file
from ..eventlog import EventLog
from ..registry import load as load_lab
from ..slackio import SILENT, SlackIO
from ..worker.approver import BrokerApprover
from ..worker.runner import NoChannelApprover
from ..worker.service import _read_secret
from ..worker.usage import UsageLog, probe_loop, probe_now
from .brain import TURN_BUDGET_S, Brain, guest_notice
from .followup import finished_prompt, follow
from .memory import Memory
from .state import Conversation, ManagerState
from .tools import BrokerClient
from .workers import WorkerError, Workers

CONFIG = "~/.config/agentic-lab"
STATE = "~/.local/state/agentic-lab"
log = logging.getLogger("agentic_lab.manager")


def compose(lab, msg, guest_thread=None):
    """The prompt for one Slack message. `guest_thread`: for a guest's turn in a shared
    thread, the thread as everyone sees it (its own session starts from that)."""
    who = (lab.humans.get(msg.human) or {}).get("name") or msg.human
    parts = []
    if guest_thread is not None:
        parts.append("The thread so far (what everyone in it can see):\n" +
                     ("\n".join(guest_thread) or "(nothing yet)"))
    elif msg.history:
        parts.append("Earlier in this thread (before you were mentioned):\n" +
                     "\n".join(msg.history))
    parts.append(f"[{who} ({msg.human})]: {msg.text}")
    if msg.files:
        parts.append("Attached files, saved in your working folder: " + ", ".join(msg.files))
    if not msg.mentioned:
        parts.append(f"(This thread message wasn't addressed to you. Answer only if it's "
                     f"useful from you; otherwise reply exactly {SILENT}.)")
    return "\n\n".join(parts)


MEANWHILE_MAX = 20      # guest lines kept for the owner's next turn

CATCH_UP = ("[lab] While you weren't running, these direct messages came here (oldest "
            "first; you haven't seen or answered them). Answer them along with the new one "
            "where they still need it:\n")

BUSY = ("Still working on the previous message here; I'll read this right after "
        "(a few minutes at most).")


class Manager:
    def __init__(self, lab, agent_id, slack, brain, workers, broker, state, memory, workdir):
        self.lab, self.agent_id = lab, agent_id
        self.slack, self.brain, self.workers, self.broker = slack, brain, workers, broker
        self.state, self.memory, self.workdir = state, memory, workdir
        self._caught_up = set()         # DMs caught up on since this manager started
        self._locks = {}
        self._told_busy = set()         # conversations told "still working" this busy spell

    def may_use(self, human):
        """Every lab member may talk to any manager; for a dedicated manager, other members
        are guests (is_guest): conversation only, no work."""
        return True, ""

    def is_guest(self, human):
        a = self.lab.agents[self.agent_id]
        return a.get("access", "dedicated") == "dedicated" and human != a.get("human")

    def mention(self, human):
        uid = (self.lab.humans.get(human) or {}).get("slack_user")
        return f"<@{uid}>" if uid else human

    async def describe_workers(self):
        out = []
        for w in self.workers.known():
            try:
                h = await self.workers.call(w, "health_check")
                out.append({"worker": w, "up": True, "tasks": h.get("tasks"), "gpu": h.get("gpu")})
            except WorkerError as e:
                out.append({"worker": w, "up": False, "error": str(e)})
        return out

    def _ctx(self, conv, requester, private):
        owner = self.lab.agents[self.agent_id].get("human")
        return SimpleNamespace(workers=self.workers, broker=self.broker, state=self.state,
                               conv=conv, requester=requester, private=private,
                               memory=self.memory, workdir=self.workdir, slack=self.slack,
                               describe_workers=self.describe_workers, lab=self.lab,
                               guest=self.is_guest(requester),
                               owner_name=(self.lab.humans.get(owner) or {}).get("name") or owner,
                               guest_name=(self.lab.humans.get(requester) or {}).get("name")
                               or requester)

    async def _catch_up(self, conv, ts):
        """A DM's new message `ts`: on the first one since this manager started, the DMs
        missed since the last one a turn saw (none if it never saw one here). Marks `ts`
        seen."""
        note, seen = "", conv.last_seen_ts
        if conv.key not in self._caught_up:
            self._caught_up.add(conv.key)
            missed = getattr(self.slack, "missed_dms", None)
            if seen is not None and missed is not None:
                lines = await missed(conv.channel, conv.thread_ts, seen, ts)
                if lines:
                    note = CATCH_UP + "\n".join(lines) + "\n\n"
        if seen is None or float(ts) > float(seen):
            conv.last_seen_ts = ts
            self.state.save()
        return note

    async def _turn(self, conv, requester, private, prompt, dm_ts=None):
        lock = self._locks.setdefault(conv.key, asyncio.Lock())
        async with lock:                # one turn at a time per conversation
            self._told_busy.discard(conv.key)
            conv.last_requester = requester
            ctx = self._ctx(conv, requester, private)
            if ctx.guest and not private:
                # a guest in the owner's thread: a session of its own, from what the thread
                # shows; never the owner's session (its tools' outputs, files, memory)
                ctx.conv = Conversation(f"{conv.key}#guest", conv.channel, conv.thread_ts)
                prompt = guest_notice(ctx.owner_name, ctx.guest_name) + "\n\n" + prompt
            elif not ctx.guest and conv.meanwhile:
                prompt = ("[lab] Meanwhile in this thread (guest turns):\n"
                          + "\n".join(conv.meanwhile) + "\n\n" + prompt)
                conv.meanwhile = []
                self.state.save()
            if dm_ts is not None:       # in turn order, so a DM is never marked seen early
                prompt = await self._catch_up(conv, dm_ts) + prompt
            try:
                reply = await self.brain.turn(ctx, prompt)
            except Exception as e:
                log.exception("turn failed in %s", conv.key)
                return f"Sorry, something went wrong on my side ({type(e).__name__})."
            if ctx.guest and not private and reply and reply.strip() != SILENT:
                conv.meanwhile = (conv.meanwhile + [f"you (to {ctx.guest_name}, a guest): "
                                                    f"{reply[:500]}"])[-MEANWHILE_MAX:]
                self.state.save()
            return reply

    async def _guest_thread(self, msg):
        """The thread before this message, as everyone sees it (first and last messages)."""
        history = getattr(self.slack, "thread_history", None)
        if history is None or msg.thread_ts is None:
            return msg.history or []
        return await history(msg.channel, msg.thread_ts, msg.ts)

    async def on_message(self, msg):
        conv = self.state.conversation(msg.channel, msg.thread_ts)
        if msg.mentioned:
            await self.slack.react(msg.channel, msg.ts, "eyes")
            lock = self._locks.get(conv.key)
            if lock is not None and lock.locked() and conv.key not in self._told_busy:
                self._told_busy.add(conv.key)       # once per busy spell, not per message
                await self.slack.post(conv.channel, conv.thread_ts, BUSY)
        if not msg.private and self.is_guest(msg.human):     # a guest in a shared thread
            prompt = compose(self.lab, msg, await self._guest_thread(msg))
            who = (self.lab.humans.get(msg.human) or {}).get("name") or msg.human
            conv.meanwhile = (conv.meanwhile + [f"{who} (guest): {msg.text[:500]}"]
                              )[-MEANWHILE_MAX:]
        else:
            prompt = compose(self.lab, msg)
        reply = await self._turn(conv, msg.human, msg.private, prompt,
                                 dm_ts=msg.ts if msg.private else None)
        if msg.mentioned:
            await self.slack.react(msg.channel, msg.ts, "eyes", add=False)
        await self.slack.post(conv.channel, conv.thread_ts, reply)

    async def post(self, conv, text):
        await self.slack.post(conv.channel, conv.thread_ts, text)

    async def finished(self, conv, task, view):
        """The wrap-up of a finished task (posted by the follow-up, which retries a failed
        post without a new turn)."""
        private = conv.channel.startswith("D")
        return await self._turn(conv, task.requester, private, finished_prompt(task, view))


async def serve(lab_path, agent_id, host=None, port=None):
    lab = load_lab(lab_path)
    a = lab.agents.get(agent_id) or {}
    if a.get("role") != "manager":
        raise SystemExit(f"{agent_id!r} is not a manager in {lab_path}")
    slack_env = _env_file(_read_secret(f"{CONFIG}/slack.env"))
    if not (slack_env.get("SLACK_BOT_TOKEN") and slack_env.get("SLACK_APP_TOKEN")):
        raise SystemExit("no Slack tokens in ~/.config/agentic-lab/slack.env")
    worker_tokens = yaml.safe_load(_read_secret(f"{CONFIG}/workers.yaml") or "") or {}
    workers = Workers({w: lab.worker_url(w) for w in worker_tokens if w in lab.agents},
                      worker_tokens)
    broker_cfg = yaml.safe_load(_read_secret(f"{CONFIG}/broker.yaml") or "") or {}
    if broker_cfg.get("url") and broker_cfg.get("token"):
        broker = BrokerClient(broker_cfg["url"], broker_cfg["token"])
        approver = BrokerApprover(broker_cfg["url"], broker_cfg["token"])
    else:
        log.warning("no broker configured: writes outside the sandbox will be denied")
        broker, approver = None, NoChannelApprover()
    env = {}
    seat = _read_secret(f"{CONFIG}/claude-oauth-token")
    if seat:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = seat
    env.update(GH.env_for(lab, agent_id))      # its GitHub bot, if it has one

    workdir = os.path.expanduser(a.get("workdir", "~/work"))
    os.makedirs(workdir, exist_ok=True)
    from .. import safefs
    safefs.pin(workdir)         # resolved once: its file operations stay there
    tmpdir = lab.agent_tmpdir(agent_id)      # hosts with readers: a tmp they can find (#43)
    if tmpdir:
        os.makedirs(tmpdir, exist_ok=True)
        env["TMPDIR"] = tmpdir
    state_root = os.path.expanduser(STATE)
    state = ManagerState(state_root)
    memory = Memory(os.path.join(state_root, "memory"), shared=a.get("access") == "shared")
    usage = UsageLog(os.path.join(state_root, "usage.json"))
    events = EventLog(os.path.join(state_root, "events"))
    events.prune()

    def settings_for(grants):
        return lab.agent_settings(agent_id, extra_grants=grants,
                                  now=dt.datetime.now(dt.timezone.utc))

    brain = Brain(lab.display_name(agent_id), lab.name, workdir, settings_for,
                  approver=approver, model=a.get("model"), env=env, usage=usage, events=events,
                  prompt_extra=lambda: GH.instructions(lab, agent_id),
                  turn_budget_s=60 * a.get("turn_budget_min", TURN_BUDGET_S // 60))
    members = {(h or {}).get("slack_user"): hid for hid, h in lab.humans.items()
               if (h or {}).get("slack_user")}
    mgr = Manager(lab, agent_id, None, brain, workers, broker, state, memory, workdir)
    slack = SlackIO(slack_env["SLACK_BOT_TOKEN"], slack_env["SLACK_APP_TOKEN"], members,
                    mgr.may_use, mgr.on_message, os.path.join(workdir, "inbox"),
                    engaged=state.engaged)
    mgr.slack = slack

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await slack.start()
    log.info("manager %s (%s): %d worker(s), broker=%s, %d member(s) known",
             agent_id, lab.display_name(agent_id), len(workers.known()), bool(broker),
             len(members))
    GH.start(lab, agent_id, stop)       # a GitHub App identity: keep its token fresh
    follower = asyncio.create_task(follow(
        state, workers, mgr.post, mgr.finished, stop, mention=mgr.mention,
        register=(lambda t: broker.register_task(t.worker, t.task_id, t.requester))
        if broker is not None else None))
    status_server = None
    if host and port:               # the secretary's view: usage and events
        import uvicorn

        from .status import app as status_app
        status_server = uvicorn.Server(uvicorn.Config(
            status_app(_read_secret(f"{CONFIG}/monitor-token"), usage, events,
                       check=(lambda: probe_now(usage, seat)) if seat else None,
                       github=lambda: GH.problems(lab, agent_id)),
            host=host, port=port, log_level="warning"))
        status_server.install_signal_handlers = lambda: None   # this process handles signals
        asyncio.create_task(status_server.serve())
    prober = asyncio.create_task(probe_loop(usage, seat, stop))
    await stop.wait()
    if status_server is not None:
        status_server.should_exit = True
    await slack.stop()
    follower.cancel()
    prober.cancel()


class ConsoleIO:
    """Stands in for Slack in `lab manager ask`: everything is printed."""

    def __init__(self):
        self.engaged = set()

    async def post(self, channel, thread_ts, text):
        if text and text.strip() != SILENT:
            print(f"\n[{channel}] {text}", flush=True)

    async def upload(self, channel, thread_ts, path, title, comment=None):
        print(f"\n[{channel}] (file posted: {title} <- {path}){' ' + comment if comment else ''}",
              flush=True)

    async def react(self, *a, **kw):
        pass


async def ask(lab_path, agent_id, human, text, wait_s=900):
    """One message to a manager from the command line, as `human`; the reply, task
    progress and results are printed. Uses its own state (`cli/`), so it can run while the
    service does. For admins (checking a manager works) and for tests."""
    from ..slackio import Incoming
    lab = load_lab(lab_path)
    a = lab.agents.get(agent_id) or {}
    if a.get("role") != "manager":
        raise SystemExit(f"{agent_id!r} is not a manager in {lab_path}")
    if human not in lab.humans:
        raise SystemExit(f"no human {human!r} in {lab_path}")
    worker_tokens = yaml.safe_load(_read_secret(f"{CONFIG}/workers.yaml") or "") or {}
    workers = Workers({w: lab.worker_url(w) for w in worker_tokens if w in lab.agents},
                      worker_tokens)
    broker_cfg = yaml.safe_load(_read_secret(f"{CONFIG}/broker.yaml") or "") or {}
    broker = approver = None
    if broker_cfg.get("url") and broker_cfg.get("token"):
        broker = BrokerClient(broker_cfg["url"], broker_cfg["token"])
        approver = BrokerApprover(broker_cfg["url"], broker_cfg["token"])
    env = {}
    seat = _read_secret(f"{CONFIG}/claude-oauth-token")
    if seat:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = seat
    env.update(GH.env_for(lab, agent_id))      # its GitHub bot, if it has one
    workdir = os.path.expanduser(a.get("workdir", "~/work"))
    os.makedirs(workdir, exist_ok=True)
    from .. import safefs
    safefs.pin(workdir)         # resolved once: its file operations stay there
    state_root = os.path.expanduser(STATE)
    state = ManagerState(os.path.join(state_root, "cli"))
    memory = Memory(os.path.join(state_root, "memory"), shared=a.get("access") == "shared")

    def settings_for(grants):
        return lab.agent_settings(agent_id, extra_grants=grants,
                                  now=dt.datetime.now(dt.timezone.utc))

    brain = Brain(lab.display_name(agent_id), lab.name, workdir, settings_for,
                  approver=approver or NoChannelApprover(), model=a.get("model"), env=env,
                  prompt_extra=lambda: GH.instructions(lab, agent_id),
                  turn_budget_s=60 * a.get("turn_budget_min", TURN_BUDGET_S // 60))
    io = ConsoleIO()
    mgr = Manager(lab, agent_id, io, brain, workers, broker, state, memory, workdir)
    ok, why = mgr.may_use(human)
    if not ok:
        raise SystemExit(why)
    await mgr.on_message(Incoming("cli", None, "0", None, human, text, True, True))
    stop = asyncio.Event()
    GH.start(lab, agent_id, stop)       # a GitHub App identity: keep its token fresh
    follower = asyncio.create_task(follow(state, workers, mgr.post, mgr.finished, stop,
                                          mention=lambda h: h))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while state.open_tasks() and loop.time() < deadline:
        await asyncio.sleep(2)
    await asyncio.sleep(1)
    stop.set()
    await follower
    if state.open_tasks():
        print(f"\n(still running after {wait_s}s: "
              f"{', '.join(t.task_id for t in state.open_tasks())})")
