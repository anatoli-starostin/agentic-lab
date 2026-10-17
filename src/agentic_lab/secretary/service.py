"""`lab secretary serve`: the secretary's service (DESIGN.md §4.1).

Collects the lab's state every few seconds and serves the web UI on the tailnet. With its
Slack app configured, it also talks in Slack (DMs, and channel threads it's tagged in),
asks members for secrets, and posts notices (expiring credentials) to the lab's
announcements channel. Secrets, readable by this service only:

    ~/.config/agentic-lab/monitor.yaml        {worker-id: read-only token}
    ~/.config/agentic-lab/broker.yaml         {url, token} (read-only overview)
    ~/.config/agentic-lab/slack.env           its Slack app (optional)
    ~/.config/agentic-lab/claude-oauth-token  the seat token, for its chat agent
"""
import asyncio
import json
import logging
import os
import tempfile

import uvicorn
import yaml

from ..registry import load as load_lab
from ..worker.service import _read_secret
from ..manager.workers import Workers
from ..broker.service import _env_file
from ..manager.state import ManagerState
from ..secrets.intake import Intake, message
from ..slackio import SILENT, SlackIO
from .brain import SecretaryBrain, expiry_notices
from ..eventlog import EventLog
from ..worker.usage import UsageLog
from . import power as P
from .status import collect, manager_status
from .web import app
import datetime as dt
from types import SimpleNamespace

CONFIG = "~/.config/agentic-lab"
STATE = "~/.local/state/agentic-lab"
COLLECT_EVERY_S = 15
log = logging.getLogger("agentic_lab.secretary")


INTAKE_PORT = 8771


class Secretary:
    """The secretary's Slack side: answers members, asks them for secrets, posts notices."""

    def __init__(self, lab, latest, workers, brain, state, slack=None):
        self.lab, self.latest, self.workers = lab, latest, workers
        self.brain, self.state, self.slack = brain, state, slack
        self._locks = {}
        self.events = None
        sec = next(a for a, x in lab.agents.items() if x.get("role") == "secretary")
        name, _ = lab.address(sec)
        self.intake_url = f"http://{name}:{INTAKE_PORT}"
        self.intake = Intake(os.path.expanduser(STATE))     # the same store the intake serves

    def ctx(self):
        return SimpleNamespace(state=lambda: self.latest["state"], workers=self.workers,
                               request_token=self.request_token, announce=self.announce,
                               save=self.state.save, power=self.power)

    async def power(self, host, action, why="asked in Slack"):
        try:
            if action == "status":
                return f"{host}: {await P.status(self.lab, host)}"
            if action == "on":
                await P.start(self.lab, host)
            else:
                await P.stop(self.lab, host)
        except P.PowerError as e:
            return f"couldn't: {e}"
        if self.events is not None:
            self.events.add("power", f"{host} {action} ({why})")
        await self.announce(f":electric_plug: {host} powering {action} ({why}).")
        return f"{host}: powering {action}; it takes a minute or two"

    async def request_token(self, member):
        if member not in self.lab.humans:
            return f"no member {member!r} in lab.yaml"
        h = self.lab.humans[member] or {}
        code = self.intake.request(member, "claude-token")
        text = message(h.get("name") or member, code, self.intake_url)
        if self.slack is None or not h.get("slack_user"):
            return f"no Slack for {member}; give them this message:\n{text}"
        im = await self.slack.web.conversations_open(users=h["slack_user"])
        await self.slack.post(im["channel"]["id"], None, text)
        return f"sent {member} a DM with a one-time command (valid 24 h)"

    async def announce(self, text):
        channel = self.lab.data.get("announcements")
        if not channel or self.slack is None:
            return False
        await self.slack.post(channel.lstrip("#"), None, text)
        return True

    async def on_message(self, msg):
        conv = self.state.conversation(msg.channel, msg.thread_ts)
        lock = self._locks.setdefault(conv.key, asyncio.Lock())
        who = (self.lab.humans.get(msg.human) or {}).get("name") or msg.human
        prompt = "\n\n".join(filter(None, [
            ("Earlier in this thread:\n" + "\n".join(msg.history)) if msg.history else "",
            f"[{who} ({msg.human})]: {msg.text}",
            "" if msg.mentioned else f"(Not addressed to you: answer only if useful from "
                                     f"you, otherwise reply exactly {SILENT}.)"]))
        async with lock:
            try:
                reply = await self.brain.turn(self.ctx(), conv, prompt)
            except Exception as e:
                log.exception("secretary turn failed")
                reply = f"Sorry, something went wrong on my side ({type(e).__name__})."
        if self.slack is not None:
            await self.slack.post(conv.channel, conv.thread_ts, reply)
        return reply

    async def notices(self):
        """Once a day: credentials expiring within 14 days, announced (and the owner of a
        Claude token gets a DM with a fresh one-time command)."""
        today = dt.date.today().isoformat()
        done = getattr(self.state, "noticed", None) or {}
        for cred, left in expiry_notices(self.lab):
            key = f"{cred['name']}:{today}"
            if done.get(key):
                continue
            when = "expired" if left < 0 else f"expires in {left} days"
            await self.announce(f":hourglass: *{cred['name']}* {when} ({cred['expires']}).")
            done[key] = True
        self.state.noticed = done


async def serve(lab_path, host, port):
    lab = load_lab(lab_path)
    mon = yaml.safe_load(_read_secret(f"{CONFIG}/monitor.yaml") or "") or {}
    role = lambda a: lab.agents.get(a, {}).get("role")     # noqa: E731
    workers = Workers({w: lab.worker_url(w) for w in mon if role(w) == "worker"},
                      {w: t for w, t in mon.items() if role(w) == "worker"}, timeout=15)
    managers = {m: t for m, t in mon.items() if role(m) == "manager"}
    bcfg = yaml.safe_load(_read_secret(f"{CONFIG}/broker.yaml") or "") or {}
    broker = (bcfg["url"], bcfg["token"]) if bcfg.get("url") and bcfg.get("token") else None
    events = EventLog(os.path.expanduser(f"{STATE}/events"))
    events.prune()
    usage = UsageLog(os.path.expanduser(f"{STATE}/usage.json"))
    sec_id = next(a for a, x in lab.agents.items() if x.get("role") == "secretary")
    admins = {(h or {}).get("tailscale_login") for h in lab.humans.values()
              if (h or {}).get("admin") and (h or {}).get("tailscale_login")}

    async def get_events(agent, hours):
        r = role(agent)
        if r == "worker":
            return (await workers.call(agent, "recent_events", {"hours": hours}))["events"]
        if r == "manager" and agent in managers:
            return (await manager_status(lab, agent, managers[agent], hours))["events"]
        if r == "secretary":
            import datetime as _dt
            return events.read(since=_dt.timedelta(hours=hours), limit=2000)
        raise ValueError(f"no log for {agent!r}")

    def lab_host_ips():
        """The tailnet addresses of the lab's own hosts: agents run there, never admins."""
        import socket
        ips = set()
        for hid in lab.hosts:
            try:
                ips.add(socket.gethostbyname(lab.address_of_host(hid)))
            except OSError:
                pass
        return ips

    async def is_admin(ip):
        """Who's behind this tailnet address? A person (not a tagged machine, not one of
        the lab's hosts) whose login is an admin's in lab.yaml?"""
        if ip in lab_host_ips():
            return False
        try:
            p = await asyncio.create_subprocess_exec(
                "tailscale", "whois", "--json", ip, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 10)
            who = json.loads(out or b"{}")
        except (OSError, ValueError, asyncio.TimeoutError):
            return False
        if (who.get("Node") or {}).get("Tags"):
            return False
        login = (who.get("UserProfile") or {}).get("LoginName")
        return bool(login) and login in admins
    here = {aid for aid, a in lab.agents.items()
            if a.get("host") == next((x.get("host") for x in lab.agents.values()
                                      if x.get("role") == "secretary"), None)}
    units = {aid: {"worker": f"lab-worker@{aid}", "manager": f"lab-manager@{aid}"}.get(
        lab.agents[aid].get("role")) for aid in here}
    units = {k: v for k, v in units.items() if v}
    snapshot_path = os.path.expanduser(f"{STATE}/lab-state.json")
    os.makedirs(os.path.dirname(snapshot_path), mode=0o700, exist_ok=True)
    latest = {"state": None}
    try:
        with open(snapshot_path) as f:
            latest["state"] = json.load(f)
    except (OSError, ValueError):
        pass

    # uvicorn handles SIGTERM/SIGINT (a graceful shutdown); when it returns, the rest stops
    stop = asyncio.Event()

    idle = P.IdleWatch()
    hosts_power = {}
    rounds = {"n": 0}

    async def check_power():
        """Every few rounds: the power state of the machines the secretary may power, and
        stopping the on-demand ones that have been idle long enough."""
        for host in lab.hosts:
            if not P.powerable(lab, host)[0]:
                continue
            try:
                hosts_power[host] = {"state": await P.status(lab, host),
                                     "at": dt.datetime.now(dt.timezone.utc).isoformat(
                                         timespec="seconds")}
            except P.PowerError as e:
                hosts_power[host] = {"state": "unknown", "error": str(e)[:200]}
        for host in idle.candidates(lab, latest["state"] or {"agents": {}}):
            if hosts_power.get(host, {}).get("state") == "RUNNING":
                await sec.power(host, "off", f"idle for {lab.hosts[host]['idle_stop_min']} min")
                idle.idle_since.pop(host, None)

    async def collector():
        while not stop.is_set():
            try:
                latest["state"] = await collect(lab, workers, broker, units, managers,
                                                own=(sec_id, usage))
                if rounds["n"] % 4 == 0:
                    await check_power()
                rounds["n"] += 1
                latest["state"]["hosts"] = {
                    h: {**(hosts_power.get(h) or {}), "power": (lab.hosts[h] or {}).get("power"),
                        "idle_since": (idle.idle_since.get(h).isoformat(timespec="seconds")
                                       if idle.idle_since.get(h) else None)}
                    for h in lab.hosts}
                fd, tmp = tempfile.mkstemp(dir=os.path.dirname(snapshot_path))
                with os.fdopen(fd, "w") as f:
                    json.dump(latest["state"], f)
                os.replace(tmp, snapshot_path)
            except Exception:
                log.exception("collecting the lab's state failed")
            try:
                await asyncio.wait_for(stop.wait(), COLLECT_EVERY_S)
            except asyncio.TimeoutError:
                pass

    sec_id = next(a for a, x in lab.agents.items() if x.get("role") == "secretary")
    sa = lab.agents[sec_id]
    env = {}
    seat = _read_secret(f"{CONFIG}/claude-oauth-token")
    if seat:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = seat
    workdir = os.path.expanduser(sa.get("workdir", "~/work"))
    os.makedirs(workdir, exist_ok=True)
    brain = SecretaryBrain(lab.display_name(sec_id), lab.name, workdir,
                           lab.agent_settings(sec_id), model=sa.get("model"), env=env,
                           events=events, usage=usage)
    sec = Secretary(lab, latest, workers, brain,
                    ManagerState(os.path.expanduser(f"{STATE}/chat")))
    sec.events = events
    slack_env = _env_file(_read_secret(f"{CONFIG}/slack.env"))
    if slack_env.get("SLACK_BOT_TOKEN") and slack_env.get("SLACK_APP_TOKEN"):
        members = {(h or {}).get("slack_user"): hid for hid, h in lab.humans.items()
                   if (h or {}).get("slack_user")}
        sec.slack = SlackIO(slack_env["SLACK_BOT_TOKEN"], slack_env["SLACK_APP_TOKEN"],
                            members, lambda human: (True, ""), sec.on_message,
                            os.path.join(workdir, "inbox"), engaged=sec.state.engaged)
        await sec.slack.start()

    async def notice_loop():
        while not stop.is_set():
            try:
                await sec.notices()
            except Exception:
                log.exception("notices failed")
            try:
                await asyncio.wait_for(stop.wait(), 3600)
            except asyncio.TimeoutError:
                pass

    server = uvicorn.Server(uvicorn.Config(app(lambda: latest["state"], get_events, is_admin),
                                           host=host, port=port, log_level="warning"))
    log.info("secretary: watching %d worker(s), slack=%s, web UI on http://%s:%d/",
             len(workers.known()), sec.slack is not None, host, port)
    tasks = [asyncio.create_task(collector()), asyncio.create_task(notice_loop())]
    await server.serve()
    stop.set()
    if sec.slack is not None:
        await sec.slack.stop()
    for t in tasks:
        t.cancel()


async def ask(lab_path, human, text):
    """One message to the secretary from the command line, as `human` (no Slack)."""
    from ..slackio import Incoming
    lab = load_lab(lab_path)
    mon = yaml.safe_load(_read_secret(f"{CONFIG}/monitor.yaml") or "") or {}
    workers = Workers({w: lab.worker_url(w) for w in mon if w in lab.agents}, mon, timeout=15)
    try:
        with open(os.path.expanduser(f"{STATE}/lab-state.json")) as f:
            latest = {"state": json.load(f)}
    except (OSError, ValueError):
        latest = {"state": None}
    sec_id = next(a for a, x in lab.agents.items() if x.get("role") == "secretary")
    sa = lab.agents[sec_id]
    env = {}
    seat = _read_secret(f"{CONFIG}/claude-oauth-token")
    if seat:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = seat
    workdir = os.path.expanduser(sa.get("workdir", "~/work"))
    os.makedirs(workdir, exist_ok=True)
    brain = SecretaryBrain(lab.display_name(sec_id), lab.name, workdir,
                           lab.agent_settings(sec_id), model=sa.get("model"), env=env,
                           usage=UsageLog(os.path.expanduser(f"{STATE}/usage.json")))
    sec = Secretary(lab, latest, workers, brain,
                    ManagerState(os.path.expanduser(f"{STATE}/chat-cli")))
    print(await sec.on_message(Incoming("cli", None, "0", None, human, text, True, True)))
