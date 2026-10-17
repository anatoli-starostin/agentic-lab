"""`lab manager serve <agent>`: a manager as a system service (DESIGN.md §4.3).

Runs as the manager's own Unix user. Its secrets (readable by the service, never by the
agent):

    ~/.config/agentic-lab/slack.env            the manager's Slack app: SLACK_BOT_TOKEN, SLACK_APP_TOKEN
    ~/.config/agentic-lab/workers.yaml         {worker-id: token} to reach workers
    ~/.config/agentic-lab/broker.yaml          {url, token} for approvals
    ~/.config/agentic-lab/claude-oauth-token   the seat token
"""
import asyncio
import datetime as dt
import logging
import os
import signal
from types import SimpleNamespace

import yaml

from ..broker.service import _env_file
from ..registry import load as load_lab
from ..slackio import SILENT, SlackIO
from ..worker.approver import BrokerApprover
from ..worker.runner import NoChannelApprover
from ..worker.service import _read_secret
from ..worker.usage import UsageLog
from .brain import Brain
from .followup import finished_prompt, follow
from .memory import Memory
from .state import ManagerState
from .tools import BrokerClient
from .workers import WorkerError, Workers

CONFIG = "~/.config/agentic-lab"
STATE = "~/.local/state/agentic-lab"
log = logging.getLogger("agentic_lab.manager")


def compose(lab, msg):
    """The prompt for one Slack message."""
    who = (lab.humans.get(msg.human) or {}).get("name") or msg.human
    parts = []
    if msg.history:
        parts.append("Earlier in this thread (before you were mentioned):\n" +
                     "\n".join(msg.history))
    parts.append(f"[{who} ({msg.human})]: {msg.text}")
    if msg.files:
        parts.append("Attached files, saved in your working folder: " + ", ".join(msg.files))
    if not msg.mentioned:
        parts.append(f"(This thread message wasn't addressed to you. Answer only if it's "
                     f"useful from you; otherwise reply exactly {SILENT}.)")
    return "\n\n".join(parts)


class Manager:
    def __init__(self, lab, agent_id, slack, brain, workers, broker, state, memory, workdir):
        self.lab, self.agent_id = lab, agent_id
        self.slack, self.brain, self.workers, self.broker = slack, brain, workers, broker
        self.state, self.memory, self.workdir = state, memory, workdir
        self._locks = {}

    def may_use(self, human):
        a = self.lab.agents[self.agent_id]
        if a.get("access", "dedicated") == "dedicated" and human != a.get("human"):
            return False, (f"I'm {self.lab.display_name(self.agent_id)}, dedicated to "
                           f"{a.get('human')}. Ask your own manager, or the lab's admin.")
        return True, ""

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
        return SimpleNamespace(workers=self.workers, broker=self.broker, state=self.state,
                               conv=conv, requester=requester, private=private,
                               memory=self.memory, workdir=self.workdir, slack=self.slack,
                               describe_workers=self.describe_workers)

    async def _turn(self, conv, requester, private, prompt):
        lock = self._locks.setdefault(conv.key, asyncio.Lock())
        async with lock:                # one turn at a time per conversation
            conv.last_requester = requester
            try:
                return await self.brain.turn(self._ctx(conv, requester, private), prompt)
            except Exception as e:
                log.exception("turn failed in %s", conv.key)
                return f"Sorry, something went wrong on my side ({type(e).__name__})."

    async def on_message(self, msg):
        conv = self.state.conversation(msg.channel, msg.thread_ts)
        if msg.mentioned:
            await self.slack.react(msg.channel, msg.ts, "eyes")
        reply = await self._turn(conv, msg.human, msg.private, compose(self.lab, msg))
        if msg.mentioned:
            await self.slack.react(msg.channel, msg.ts, "eyes", add=False)
        await self.slack.post(conv.channel, conv.thread_ts, reply)

    async def post(self, conv, text):
        await self.slack.post(conv.channel, conv.thread_ts, text)

    async def finished(self, conv, task, view):
        private = conv.channel.startswith("D")
        reply = await self._turn(conv, task.requester, private, finished_prompt(task, view))
        await self.slack.post(conv.channel, conv.thread_ts, reply)


async def serve(lab_path, agent_id):
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

    workdir = os.path.expanduser(a.get("workdir", "~/work"))
    os.makedirs(workdir, exist_ok=True)
    state_root = os.path.expanduser(STATE)
    state = ManagerState(state_root)
    memory = Memory(os.path.join(state_root, "memory"), shared=a.get("access") == "shared")
    usage = UsageLog(os.path.join(state_root, "usage.json"))

    def settings_for(grants):
        return lab.agent_settings(agent_id, extra_grants=grants,
                                  now=dt.datetime.now(dt.timezone.utc))

    brain = Brain(lab.display_name(agent_id), lab.name, workdir, settings_for,
                  approver=approver, model=a.get("model"), env=env, usage=usage)
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
    follower = asyncio.create_task(follow(state, workers, mgr.post, mgr.finished, stop,
                                          mention=mgr.mention))
    await stop.wait()
    await slack.stop()
    follower.cancel()
