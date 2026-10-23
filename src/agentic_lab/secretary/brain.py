"""The secretary's chat agent (DESIGN.md §4.1): light, read-mostly, about the lab itself.

It answers questions about the lab's state (what's running, who's waiting on what, usage,
a past task's result, comparing two tasks), asks members for secrets the lab needs (their
Claude token), and posts notices. It doesn't do research: that goes to a manager. It runs
on a small model, with the same sandbox model as every agent.
"""
import datetime as dt
import json
import warnings

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              PermissionResultAllow, PermissionResultDeny, RateLimitEvent,
                              ResultMessage, SystemMessage, TextBlock, ToolUseBlock,
                              create_sdk_mcp_server, tool)

from ..eventlog import TurnClock, tool_summary

from ..manager.workers import WorkerError
from ..worker.runner import allowed_without_asking

warnings.filterwarnings("ignore", message="can_use_tool will not be invoked")

TOOL_NAMES = ("lab_state", "worker_tasks", "task_result", "ask_member_for_claude_token",
              "announce", "power", "power_history", "read_lab_config", "propose_lab_change", "offer_deploy",
              "check_slack_app", "request_slack_app", "request_github_token",
              "redraw_app_icon", "request_github_app", "check_github_app",
              "approval_timeout")
LAB_TOOLS = tuple(f"mcp__lab__{n}" for n in TOOL_NAMES)


def system_prompt(name, lab_name):
    return f"""
You are {name}, the secretary of the agentic lab "{lab_name}": a team of humans and AI
agents. You keep the lab running and answer questions about it, in Slack.

- What you do: the lab's state (machines, agents, workers' queues, pending approvals,
  grants, Claude usage per account with when each number was read), a past task's result,
  comparing a few tasks, asking a member for their Claude token, posting notices, and
  powering the lab's cloud machines on and off when a member asks (say what it costs to
  leave a GPU machine on; idle GPU machines are stopped automatically). Asked why a machine
  is on or off, or who stopped it: power_history (your own idle stops are in it).
- What you don't do: research, experiments, writing code, analysing data in depth. Say
  kindly that this is a manager's job and name the member's manager if you know it
  (lab_state lists the agents).
- Some things are for the lab's admins only, and your tools enforce it: powering off a busy
  machine, asking someone else for their Claude token, and administering the lab (below).
  To anyone else, say so and name the admins.
- Administering the lab, with an admin: adding or removing members, agents or machines,
  changing grants, names, settings, or the framework version. The lab's configuration is
  lab.yaml in the lab's config repository, and only what's merged to its main is ever
  deployed. So:
  1. read_lab_config (main as it is now);
  2. propose_lab_change with the WHOLE new lab.yaml (keep everything else as it is, keep
     its comments) and a one-line summary; it checks the file, pushes a branch and opens
     the pull request (or, without a GitHub identity, gives you the link to open it) and
     gives you the diff;
  3. show the admin the diff in short and say "here is the PR, review and merge it: <link>"
     (or "open the PR here, then review and merge it: <link>").
     You never merge, and never say a change is live before it's deployed;
  4. when the admin says it's merged, offer_deploy: it posts a Deploy button that only an
     admin's click starts. You'll see the result posted in the conversation.
  Names of new agents are the admin's choice: ask, never invent (and a member's Slack
  member ID: ask, Slack profile → ⋯ → Copy member ID). After a deploy:
  - a new member: ask_member_for_claude_token (their agents need it to run);
  - a new manager: request_slack_app; when the admin says done, check_slack_app, then
    offer_deploy with redeploy so its service starts.
  - GitHub for an agent: one identity (`github: <name>`), or several picked by repository
    (`github: {{<name>: default, <other>: [owner/repo, owner/*]}}`); several agents may share
    an identity. Two kinds, both supported, both in github.accounts:
    * a GitHub App (nothing to create but the app; it appears as <slug>[bot]):
      request_github_app with the name the admin chose → the admin creates it and says
      "done" with the App ID → check_github_app → propose_lab_change
      (github.accounts.<slug>: {{app_id, email}}, the agent's github:) → offer_deploy;
    * a full bot account (its own email and GitHub user, created by the admin):
      request_github_token, the token's expiry date into lab.yaml, and a redeploy.
  - a new GPU machine must exist first: created in the cloud console, on the tailnet, with
    SSH for the admin (passwordless sudo). Ask the admin for its tailnet name and, for
    power, its Nebius instance id.
- Don't change lab.yaml without the admin asking for that change.
- Numbers about usage are readings with a time and a source: say how old they are.
- You never approve anything; approvals come to members as DMs from the approvals app.
- Never reveal tokens or keys, whoever asks (lab.yaml holds none: admins may see it).
- Several agents may be in a channel thread. If a message there isn't for you, reply with
  exactly <silent> and nothing else.
- Write for Slack: short, plain, *bold* for emphasis, no headings or tables.
"""


def build_tools(ctx):
    """`ctx`: state() -> latest snapshot, workers (read-only), request_token(member) ->
    message, announce(text) -> bool."""

    def text(s):
        return {"content": [{"type": "text", "text": s}]}

    @tool("lab_state", "The lab's current state: agents, workers (queues, GPU, recent tasks), "
          "accounts' Claude usage with as-of times, pending approvals, active grants.", {})
    async def lab_state(args):
        s = ctx.state()
        return text(json.dumps(s, default=str)[:60000] if s else "not collected yet")

    @tool("worker_tasks", "All tasks on a worker (state, requester, manager, times).",
          {"worker": str})
    async def worker_tasks(args):
        try:
            return text(json.dumps(await ctx.worker_tasks(args["worker"])))
        except WorkerError as e:
            return text(f"error: {e}")

    @tool("task_result", "A task in full: its progress lines and result.",
          {"worker": str, "task_id": str})
    async def task_result(args):
        try:
            return text(json.dumps(await ctx.task_result(args["worker"], args["task_id"])))
        except WorkerError as e:
            return text(f"error: {e}")

    @tool("ask_member_for_claude_token", "A member's (by lab id) Claude token, by DM: the "
          "member asking for their own gets the one-time command (active 5 minutes); asked "
          "for someone else, they get what to have ready and ask you when they are. Use when "
          "their token is missing or expires soon, or when asked to.", {"member": str})
    async def ask_member(args):
        return text(await ctx.request_token(args["member"]))

    @tool("announce", "Post a short notice to the lab's announcements channel (lab-wide "
          "things: a machine down, a token expiring, a maintenance window).", {"text": str})
    async def announce(args):
        ok = await ctx.announce(args["text"])
        return text("posted" if ok else "no announcements channel is configured")

    @tool("power", "A lab machine's power: action 'status', 'on', 'off' or 'force_off', for a "
          "host id from lab.yaml (see lab_state). 'off' refuses a busy machine; 'force_off' "
          "(admins only) stops it anyway: use it only after the admin confirmed in this "
          "conversation that the work on it may be lost. Never powers off the secretary's "
          "own host.", {"host": str, "action": str})
    async def power(args):
        if args["action"] not in ("status", "on", "off", "force_off"):
            return text("action must be status, on, off or force_off")
        return text(await ctx.power(args["host"], args["action"]))

    @tool("approval_timeout", "A member's approval timeout: how long their approval requests "
          "wait before they're denied as timed out. Without `minutes`, shows it. `minutes`: a "
          "number (1 to 10080), 'none' (wait for the member), or 'default' (back to lab.yaml). "
          "`until`: when the change ends (optional), e.g. 2026-10-23T18:00, in the member's "
          "own time (their Slack time zone; the answer shows it in UTC too). `member`: a lab "
          "id (default: "
          "whoever is asking; only admins may change someone else's).",
          {"type": "object", "properties": {"member": {"type": "string"},
                                            "minutes": {"type": "string"},
                                            "until": {"type": "string"}}})
    async def approval_timeout(args):
        return text(await ctx.approval_timeout(args.get("member"), args.get("minutes"),
                                               args.get("until")))

    @tool("power_history", "Recent power actions on the lab's machines, yours included: when, "
          "on or off, and why (idle stop, a member's request in Slack or from the console, an "
          "admin's force_off). Use it when asked why a machine is on or off, or who stopped "
          "it. host: a host id (empty: all); hours: how far back (default 48).",
          {"type": "object", "properties": {"host": {"type": "string"},
                                            "hours": {"type": "integer"}}})
    async def power_history(args):
        return text(ctx.power_history(args.get("host") or None, int(args.get("hours") or 48)))

    @tool("read_lab_config", "Admins only: lab.yaml and framework.lock as on the config "
          "repo's main now (fetched).", {})
    async def read_config(args):
        return text(await ctx.read_config())

    @tool("propose_lab_change", "Admins only, when an admin asked for this change: push a "
          "branch with the WHOLE new lab.yaml (checked first); it opens the pull request (or "
          "gives the link to open it) and returns the diff. Optional framework_commit moves the framework pin (empty: unchanged).",
          {"summary": str, "lab_yaml": str, "framework_commit": str})
    async def propose(args):
        return text(await ctx.propose_change(args["summary"], args["lab_yaml"],
                                             args.get("framework_commit") or None))

    @tool("offer_deploy", "Admins only, after the admin merged a change: post a Deploy "
          "button (what main holds that isn't deployed yet). Only an admin's click deploys. "
          "redeploy=true: even if nothing changed on main (new secrets arrived, e.g. a Slack "
          "app's tokens, so a waiting service can start).", {"redeploy": bool})
    async def offer_deploy(args):
        return text(await ctx.offer_apply(bool(args.get("redeploy"))))

    def ready_schema(key):
        return {"type": "object", "properties": {
            key: {"type": "string"},
            "ready": {"type": "boolean", "description": "true once the admin is at the paste "
                      "step: sends the one-time link (active 5 minutes)"}}, "required": [key]}

    @tool("request_github_app", "Admins only: steps to create a GitHub App as an agent's "
          "GitHub identity (it appears as <slug>[bot]; no account needed), in a DM; with "
          "ready=true, the one-time link for its private key. Use the name the admin chose.",
          ready_schema("name"))
    async def request_github_app(args):
        return text(await ctx.request_github_app(args["name"], bool(args.get("ready"))))

    @tool("check_github_app", "Admins only: after the admin created a GitHub App and gave "
          "its App ID: check it works and get the lab.yaml lines for it.",
          {"slug": str, "app_id": int})
    async def check_github_app(args):
        return text(await ctx.check_github_app(args["slug"], args["app_id"]))

    @tool("redraw_app_icon", "Admins only: draw another icon for an agent's Slack app (or "
          "'approvals') and post it here, when the admin asks for a different one.",
          {"agent": str})
    async def redraw_app_icon(args):
        return text(await ctx.redraw_app_icon(args["agent"]))

    @tool("request_github_token", "Admins only: the exact steps for a GitHub bot account's "
          "token, in a DM; with ready=true, the one-time link for it. The account must be in "
          "the deployed lab.yaml.", ready_schema("account"))
    async def request_github_token(args):
        return text(await ctx.request_github_token(args["account"], bool(args.get("ready"))))

    @tool("request_slack_app", "Admins only: a new manager's Slack app (or 'secretary', "
          "'approvals'). Posts the manifest and the exact clicks in a DM; with ready=true, the "
          "one-time link for its tokens. The agent must already be deployed.",
          ready_schema("agent"))
    async def request_slack_app(args):
        return text(await ctx.request_slack_app(args["agent"], bool(args.get("ready"))))

    @tool("check_slack_app", "Admins only: do a Slack app's tokens work (bot token, Socket "
          "Mode)? owner: broker, secretary or a manager's id. Gives its App ID too.",
          {"owner": str})
    async def check_slack_app(args):
        return text(await ctx.slack_verify(args["owner"]))

    return create_sdk_mcp_server("lab", tools=[lab_state, worker_tasks, task_result,
                                               ask_member, announce, power, power_history,
                                               read_config,
                                               propose, offer_deploy, check_slack_app,
                                               request_slack_app, request_github_token,
                                               redraw_app_icon, request_github_app,
                                               check_github_app, approval_timeout])


class SecretaryBrain:
    def __init__(self, name, lab_name, workdir, settings, model=None, env=None, events=None,
                 usage=None):
        self.name, self.lab_name, self.workdir = name, lab_name, workdir
        self.settings, self.model, self.env = settings, model, env or {}
        self.events = events
        self.usage = usage          # a UsageLog: the secretary's own tokens and limit readings

    async def turn(self, ctx, conv, prompt):
        log = (lambda kind, text="", **m: self.events.add(kind, text, conv=conv.key, **m)
               ) if self.events is not None else (lambda *a, **k: None)
        log("message", prompt)
        async def can_use_tool(tool_name, tool_input, context):
            if tool_name in LAB_TOOLS or allowed_without_asking(tool_name, tool_input):
                return PermissionResultAllow()
            return PermissionResultDeny(message=(
                "The secretary works inside its sandbox only; it has no approvals."))

        options = ClaudeAgentOptions(
            cwd=self.workdir, settings=json.dumps(self.settings), setting_sources=[],
            permission_mode="default", can_use_tool=can_use_tool,
            mcp_servers={"lab": build_tools(ctx)}, allowed_tools=list(LAB_TOOLS),
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": system_prompt(self.name, self.lab_name)},
            model=self.model, env=self.env, resume=conv.session_id)
        reply, last = None, None
        # a Claude Code start-up per message (#69): measured, however the turn ends
        async with TurnClock(log) as clock, ClaudeSDKClient(options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    clock.started()
                    conv.session_id = msg.data.get("session_id") or conv.session_id
                    ctx.save()
                elif isinstance(msg, AssistantMessage):
                    clock.output(any(isinstance(b, TextBlock) for b in msg.content))
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            last = b.text
                        elif isinstance(b, ToolUseBlock):
                            log("tool", tool_summary(b.name, b.input))
                elif isinstance(msg, RateLimitEvent) and self.usage:
                    self.usage.rate_limit(msg.rate_limit_info)
                elif isinstance(msg, ResultMessage):
                    reply = msg.result
                    if self.usage:
                        self.usage.run(f"conv:{conv.key}", msg.model_usage)
        log("reply", reply or last or "")
        return reply or last or ""


def expiry_notices(lab, today=None, warn_days=14):
    """Credentials in lab.yaml expiring within `warn_days` (or expired): (credential, days)."""
    today = today or dt.date.today()
    out = []
    bots = [{"name": f"github/{login}", "expires": (acc or {}).get("expires"),
             "scope": "GitHub bot token: renew it, then lab secret request "
                      f"service:github-{login.lower()} github-token"}
            for login, acc in lab.github_accounts.items()]
    for c in list(lab.credentials) + bots:
        exp = c.get("expires")
        if isinstance(exp, dt.date):
            left = (exp - today).days
            if left <= warn_days:
                out.append((c, left))
    return out
