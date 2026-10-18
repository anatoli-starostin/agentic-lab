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
from ..secrets.intake import Intake, form_url, message
from ..slackio import SILENT, SlackIO
from .brain import SecretaryBrain, expiry_notices
from ..eventlog import EventLog
from .. import selfadmin as SA
from ..worker.usage import UsageLog, probe_loop, probe_now
from . import home as H
from . import power as P
from .status import check_seat, collect, manager_status
from .web import app
import datetime as dt
from types import SimpleNamespace

CONFIG = "~/.config/agentic-lab"
STATE = "~/.local/state/agentic-lab"
COLLECT_EVERY_S = 15
CHECK_MIN_GAP_S = 60            # the web UI's "check now": at most once a minute per seat
log = logging.getLogger("agentic_lab.secretary")


INTAKE_PORT = 8771
DASHBOARD_PORT = 8774
HOME_REFRESH_ROUNDS = 20        # Home tabs are republished every ~5 min (20 x 15 s)


def sec_id_of(lab):
    return next(a for a, x in lab.agents.items() if x.get("role") == "secretary")


class Secretary:
    """The secretary's Slack side: answers members, asks them for secrets, posts notices."""

    def __init__(self, lab, latest, workers, brain, state, slack=None):
        self.lab, self.latest, self.workers = lab, latest, workers
        self.brain, self.state, self.slack = brain, state, slack
        self._locks = {}
        self.events = None
        self.home_viewers = set()
        self.dashboard_url = f"http://{lab.address(sec_id_of(lab))[0]}:{DASHBOARD_PORT}/"
        sec = next(a for a, x in lab.agents.items() if x.get("role") == "secretary")
        name, _ = lab.address(sec)
        self.intake_url = f"http://{name}:{INTAKE_PORT}"
        self.intake = Intake(os.path.expanduser(STATE))     # the same store the intake serves

    def ctx(self, human=None, conv=None):
        """The tools' view of the lab, for one turn on behalf of `human` (a lab member), in
        conversation `conv` (where a deploy button goes)."""
        who = (self.lab.humans.get(human) or {}).get("name") or human
        return SimpleNamespace(
            state=lambda: self.latest["state"], workers=self.workers,
            request_token=lambda member: self.request_token(member, asked_by=human),
            announce=self.announce, save=self.state.save,
            power=lambda host, action: self.power(host, action, f"asked by {who} in Slack",
                                                  asked_by=human),
            read_config=lambda: self.read_config(asked_by=human),
            propose_change=lambda summary, lab_yaml, framework_commit=None:
                self.propose_change(summary, lab_yaml, framework_commit, asked_by=human),
            offer_apply=lambda redeploy=False: self.offer_apply(asked_by=human, conv=conv,
                                                                redeploy=redeploy),
            request_slack_app=lambda agent: self.request_slack_app(agent, asked_by=human,
                                                                   conv=conv),
            slack_verify=lambda owner: self.slack_verify(owner, asked_by=human),
            request_github_token=lambda account: self.request_github_token(
                account, asked_by=human, conv=conv))

    # ── administering the lab from Slack (selfadmin.py) ──────────────────────

    def _admin_only(self, human, what):
        if not self.is_admin(human):
            return f"refused: only an admin ({self.admins()}) may {what}."
        return None

    def config_copy(self):
        sa = SA.applied(SA.APPLY_MAIN)          # where lab.yaml sits in the config repo
        return SA.ConfigCopy(sa.get("lab_rel") or "lab.yaml", author=(
            f"{self.lab.display_name(sec_id_of(self.lab))} (lab secretary)",
            f"{self.lab.unix_user(sec_id_of(self.lab))}@agentic-lab"))

    async def read_config(self, asked_by=None):
        refused = self._admin_only(asked_by, "read the lab's configuration")
        if refused:
            return refused
        try:
            m = await asyncio.to_thread(self.config_copy().read_main)
        except SA.AdminError as e:
            return f"couldn't read the config repo: {e}"
        return (f"config repo main at {m['commit'][:10]}\n--- lab.yaml ---\n{m['lab.yaml']}"
                f"\n--- framework.lock ---\n{m.get('framework.lock') or '(none)'}")

    async def propose_change(self, summary, lab_yaml, framework_commit=None, asked_by=None):
        refused = self._admin_only(asked_by, "change the lab's configuration")
        if refused:
            return refused
        try:
            r = await asyncio.to_thread(self.config_copy().propose, summary, lab_yaml,
                                        framework_commit)
        except SA.AdminError as e:
            return f"not proposed: {e}"
        if self.events is not None:
            self.events.add("note", f"proposed {r['branch']}: {summary}", member=asked_by)
        return (f"pushed branch {r['branch']}. PR link (give it to the admin to review and "
                f"merge; you never merge): {r['link'] or '(not GitHub: open it by hand)'}\n"
                f"diff:\n{r['diff'][:6000]}")

    async def request_slack_app(self, agent, asked_by=None, conv=None):
        """Hand an admin what they need to create a manager's (or the secretary's, or the
        approvals') Slack app: the manifest, the link, the clicks, and a one-time link for
        its tokens. Posted as is (no retyping by the model)."""
        refused = self._admin_only(asked_by, "set up Slack apps")
        if refused:
            return refused
        from .. import slack_manifest as M
        if agent in ("broker", "approvals"):
            owner, manifest = "service:broker", M.approvals(self.lab)
        elif self.lab.agents.get(agent, {}).get("role") in ("manager", "secretary"):
            owner = f"service:{agent}"
            manifest = M.for_agent(self.lab, agent)
        else:
            return (f"{agent!r} isn't a manager or the secretary in the deployed lab.yaml: "
                    "propose it, have it merged, deploy it, then ask again")
        if self.slack is None or conv is None:
            return "this needs Slack; from the console: lab slack-manifest + lab secret request"
        code = self.intake.request(owner, "slack-app")
        name = manifest["display_information"]["name"]
        steps = (f"*Slack app for {name}* (one-time link valid 24 h)\n"
                 "1. Open https://api.slack.com/apps?new_app=1 → *From a manifest* → this "
                 "workspace → *Next*.\n"
                 "2. *YAML* tab, replace everything with the manifest below → *Next* → *Create*.\n"
                 "3. *Install App* → *Install to Workspace* → *Allow*. Copy the *Bot User "
                 "OAuth Token* (xoxb-…).\n"
                 "4. *Basic Information* → *App-Level Tokens* → *Generate Token and Scopes* → "
                 "any name → scope `connections:write` → *Generate*. Copy it (xapp-…).\n"
                 f"5. Paste both here (never in Slack): {form_url(code, self.intake_url)}\n"
                 "6. Optional, its icon: *Basic Information* → *Display Information* → *App "
                 "icon*: upload the image I post below.\n"
                 "Then tell me \"done\": I check the tokens and offer the deploy that starts it.")
        await self.slack.post(conv.channel, conv.thread_ts, steps)
        await self.slack.post(conv.channel, conv.thread_ts,
                              f"```{M.render(manifest)}```")
        try:                            # its icon, generated from its name
            import tempfile
            from ..icon import png
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                f.write(png(name))
            await self.slack.upload(conv.channel, conv.thread_ts, f.name,
                                    f"{name}-icon.png", "The app icon (step 6).")
            os.unlink(f.name)
        except Exception:
            log.exception("posting the app icon failed")
        return (f"posted the steps, the manifest and the one-time link for {name}'s app. "
                "When the admin says done: check_slack_app, then offer_deploy with redeploy.")

    async def request_github_token(self, account, asked_by=None, conv=None):
        """A one-time link for a GitHub bot account's token, with how to make one."""
        refused = self._admin_only(asked_by, "set up GitHub bot accounts")
        if refused:
            return refused
        if account not in self.lab.github_accounts:
            return (f"{account!r} isn't in github.accounts of the deployed lab.yaml: propose "
                    "it, have it merged, deploy, then ask again")
        if self.slack is None or conv is None:
            return ("this needs Slack; from the console: lab secret request "
                    f"service:github-{account.lower()} github-token")
        code = self.intake.request(f"service:github-{account.lower()}", "github-token")
        await self.slack.post(conv.channel, conv.thread_ts, (
            f"*GitHub token for the bot {account}* (one-time link valid 24 h)\n"
            f"1. Log in to GitHub *as {account}* (a private window helps).\n"
            "2. https://github.com/settings/personal-access-tokens/new (fine-grained token).\n"
            "3. Resource owner: the organisation; Repository access: only the repos the "
            "lab's agents work on.\n"
            "4. Permissions: *Contents* read and write, *Pull requests* read and write, "
            "*Issues* read and write (Metadata read comes with them). Nothing else.\n"
            "5. Expiration: up to a year; tell me the date, I'll add it to lab.yaml so I can "
            "remind you before it runs out.\n"
            f"6. Paste the token here (never in Slack): {form_url(code, self.intake_url)}\n"
            "Then: offer a redeploy, so the agents that use this bot get it.\n"
            "On GitHub, protect each repo's main: require a pull request with a review from "
            "code owners (people only), and don't let bots bypass it."))
        return (f"posted how to make {account}'s token and the one-time link. Then: the "
                "expiry date into lab.yaml (a PR), and offer_deploy with redeploy.")

    async def offer_apply(self, asked_by=None, conv=None, redeploy=False):
        """Post a 'Deploy' button for main's current commit. Nothing is deployed until an
        admin clicks it (checked again on the click). `redeploy`: even if the hosts already
        run main (new secrets arrived: a held service can start)."""
        refused = self._admin_only(asked_by, "deploy the lab's configuration")
        if refused:
            return refused
        if self.slack is None or conv is None:
            return "deploying needs Slack (a button an admin clicks); from the console: sudo lab apply"
        current = SA.applied()
        try:
            main, todo = await asyncio.to_thread(self.config_copy().not_applied,
                                                 current.get("config_commit"))
        except SA.AdminError as e:
            return f"couldn't read the config repo: {e}"
        if not todo and not redeploy:
            return (f"nothing to deploy: the hosts already run main ({main[:10]}). If new "
                    "secrets arrived (a Slack app, a token), offer a redeploy.")
        text = (f"*Deploy the lab's configuration?* main at `{main[:10]}`; "
                + ("not deployed yet:\n" + "\n".join(f"• {line}" for line in todo[:15])
                   if todo else "already deployed: a redeploy, to install new secrets")
                + "\nServices restart; it takes a few minutes.")
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
                  {"type": "actions", "elements": [
                      {"type": "button", "style": "primary", "action_id": "lab_apply",
                       "text": {"type": "plain_text", "text": "Deploy"}, "value": main},
                      {"type": "button", "action_id": "lab_apply_cancel",
                       "text": {"type": "plain_text", "text": "Not now"}, "value": main}]},
                  {"type": "context", "elements": [{"type": "mrkdwn", "text":
                      f"Only an admin's click counts ({self.admins()})."}]}]
        await self.slack.post_blocks(conv.channel, conv.thread_ts, "Deploy the lab?", blocks)
        return ("posted a Deploy button in this conversation. Tell the admin to click it; "
                "you can't deploy by yourself.")

    async def on_action(self, slack_user, human, action_id, value, where):
        if action_id not in ("lab_apply", "lab_apply_cancel"):
            return

        async def say(text):
            await self.slack.web.chat_update(channel=where["channel"], ts=where["ts"],
                                             text=text, blocks=[{"type": "section", "text": {
                                                 "type": "mrkdwn", "text": text}}])
        if not self.is_admin(human):
            await self.slack.post(where["channel"], where.get("thread_ts") or where["ts"],
                                  f"<@{slack_user}> only an admin ({self.admins()}) may "
                                  "deploy; the button is still there for them.")
            return
        who = (self.lab.humans.get(human) or {}).get("name") or human
        if action_id == "lab_apply_cancel":
            await say(f"Not deployed ({who} said not now).")
            return
        try:
            main = (await asyncio.to_thread(self.config_copy().read_main))["commit"]
            if main != value:
                await say(f"main moved to `{main[:10]}` since this button was posted: ask "
                          "me again to deploy, so you see what's in it.")
                return
            log_path = await asyncio.to_thread(SA.start_apply)
        except SA.AdminError as e:
            await say(f"Couldn't deploy: {e}")
            return
        await say(f":rocket: Deploying `{value[:10]}` (started by {who}). I'll post the "
                  "result here.")
        if self.events is not None:
            self.events.add("note", f"deploy {value[:10]} started by {human}: {log_path}")
        self._pending_applies(add={"log": log_path, "channel": where["channel"],
                                   "thread_ts": where.get("thread_ts") or where["ts"],
                                   "commit": value, "by": who})

    def _pending_applies(self, add=None, remove=None):
        path = os.path.expanduser(f"{STATE}/pending-applies.json")
        try:
            with open(path) as f:
                items = json.load(f)
        except (OSError, ValueError):
            items = []
        if add:
            items.append(add)
        if remove:
            items = [i for i in items if i["log"] != remove]
        if add or remove:
            with open(path, "w") as f:
                json.dump(items, f)
        return items

    async def check_applies(self):
        """Post the result of each deployment that finished (also after this service was
        restarted by that very deployment)."""
        for item in self._pending_applies():
            r = SA.apply_result(item["log"])
            if r is None:
                continue
            ok, tail = r
            self._pending_applies(remove=item["log"])      # report once, whatever happens next
            try:
                if self.slack is not None:
                    await self.slack.post(
                        item["channel"], item["thread_ts"],
                        (":white_check_mark: Deployed" if ok else ":x: Deploy FAILED")
                        + f" `{item['commit'][:10]}` (started by {item['by']}).\n"
                        f"```{tail[-1200:]}```")
                if ok:
                    await self.announce(f":rocket: The lab now runs config "
                                        f"`{item['commit'][:10]}` (deployed by {item['by']}).")
            except Exception:
                log.exception("reporting deployment %s failed", item["log"])

    async def slack_verify(self, owner, asked_by=None):
        refused = self._admin_only(asked_by, "check the lab's Slack apps")
        if refused:
            return refused
        from ..slack_verify import VerifyError, verify
        try:
            r = await asyncio.to_thread(verify, owner if owner.startswith("service:")
                                        else f"service:{owner}")
        except (VerifyError, OSError) as e:
            return f"check failed: {e}"
        return (f"bot token works: app {r['name']!r} in {r['team']!r}, App ID {r['app_id']}; "
                f"app-level token: {'ok (Socket Mode)' if r['socket_ok'] else r['socket_error']}")

    def is_admin(self, human):
        return bool((self.lab.humans.get(human) or {}).get("admin"))

    def admins(self):
        return " or ".join((h or {}).get("name") or hid for hid, h in self.lab.humans.items()
                           if (h or {}).get("admin")) or "the lab's admin"

    async def power(self, host, action, why="asked in Slack", asked_by=None):
        """`asked_by`: the member asking (None: the secretary itself, e.g. an idle stop).
        Powering off a busy machine is for admins only, and only with `force_off`."""
        if action in ("off", "force_off") and asked_by is not None:
            busy = P.busy(self.lab, host, self.latest["state"])
            if busy and not self.is_admin(asked_by):
                return (f"refused: {host} is busy ({busy}). Only an admin "
                        f"({self.admins()}) may power off a busy machine.")
            if busy and action != "force_off":
                return (f"{host} is busy ({busy}): powering it off loses that work. Ask the "
                        "admin to confirm, then use force_off.")
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
        verb = "on" if action == "on" else "off"
        await self.announce(f":electric_plug: {host} powering {verb} ({why}).")
        return f"{host}: powering {verb}; it takes a minute or two"

    async def request_token(self, member, asked_by=None):
        if member not in self.lab.humans:
            return f"no member {member!r} in lab.yaml"
        if asked_by is not None and asked_by != member and not self.is_admin(asked_by):
            return (f"refused: only {member} themselves or an admin ({self.admins()}) may "
                    "ask for their Claude token.")
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
                reply = await self.brain.turn(self.ctx(msg.human, conv), conv, prompt)
            except Exception as e:
                log.exception("secretary turn failed")
                reply = f"Sorry, something went wrong on my side ({type(e).__name__})."
        if self.slack is not None:
            await self.slack.post(conv.channel, conv.thread_ts, reply)
        return reply

    async def publish_home(self, slack_user):
        """Publish one person's Home tab (and remember them, to keep it fresh)."""
        if self.slack is None:
            return
        human_id = next((h for h, x in self.lab.humans.items()
                         if (x or {}).get("slack_user") == slack_user), None)
        if human_id is None:
            return
        self.home_viewers.add(slack_user)
        admin = bool((self.lab.humans[human_id] or {}).get("admin"))
        try:
            await self.slack.web.views_publish(user_id=slack_user, view=H.view(
                self.latest["state"], human_id, self.dashboard_url, admin=admin))
        except Exception:
            log.exception("publishing the Home tab for %s failed", human_id)

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
                if rounds["n"] % HOME_REFRESH_ROUNDS == 0:
                    for user in list(sec.home_viewers):
                        await sec.publish_home(user)
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
                            os.path.join(workdir, "inbox"), engaged=sec.state.engaged,
                            on_home=sec.publish_home, on_action=sec.on_action)
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

    checks = {}                     # human -> {at, agent | error}: the web UI's "check now"

    async def check_usage(human):
        last = checks.get(human)
        now = dt.datetime.now(dt.timezone.utc)
        if human not in lab.humans or (last and now - dt.datetime.fromisoformat(last["at"])
                                       < dt.timedelta(seconds=CHECK_MIN_GAP_S)):
            return
        at = now.isoformat(timespec="seconds")
        try:
            agent = await check_seat(lab, human, workers, managers,
                                     own=(sec_id, lambda: probe_now(usage, seat)) if seat else None)
            checks[human] = {"at": at, "agent": agent}
        except Exception as e:
            checks[human] = {"at": at, "error": f"check failed: {str(e)[:300]}"}
        try:
            hosts = (latest["state"] or {}).get("hosts")
            latest["state"] = await collect(lab, workers, broker, units, managers,
                                            own=(sec_id, usage))
            if hosts is not None:
                latest["state"]["hosts"] = hosts
        except Exception:
            log.exception("collecting after a usage check failed")

    def state_with_checks():
        s = latest["state"]
        return {**s, "checks": checks} if s else s

    server = uvicorn.Server(uvicorn.Config(app(state_with_checks, get_events, is_admin,
                                               check_usage),
                                           host=host, port=port, log_level="warning"))
    log.info("secretary: watching %d worker(s), slack=%s, web UI on http://%s:%d/",
             len(workers.known()), sec.slack is not None, host, port)
    async def apply_watch():
        """Deployments started from Slack: post each one's result when it finishes."""
        while not stop.is_set():
            try:
                await sec.check_applies()
            except Exception:
                log.exception("checking deployments failed")
            try:
                await asyncio.wait_for(stop.wait(), 10)
            except asyncio.TimeoutError:
                pass

    tasks = [asyncio.create_task(collector()), asyncio.create_task(notice_loop()),
             asyncio.create_task(probe_loop(usage, seat, stop)),
             asyncio.create_task(apply_watch())]
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
