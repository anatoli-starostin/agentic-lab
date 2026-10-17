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

from ..eventlog import tool_summary

from ..manager.workers import WorkerError
from ..worker.runner import allowed_without_asking

warnings.filterwarnings("ignore", message="can_use_tool will not be invoked")

TOOL_NAMES = ("lab_state", "worker_tasks", "task_result", "ask_member_for_claude_token",
              "announce")
LAB_TOOLS = tuple(f"mcp__lab__{n}" for n in TOOL_NAMES)


def system_prompt(name, lab_name):
    return f"""
You are {name}, the secretary of the agentic lab "{lab_name}": a team of humans and AI
agents. You keep the lab running and answer questions about it, in Slack.

- What you do: the lab's state (machines, agents, workers' queues, pending approvals,
  grants, Claude usage per account with when each number was read), a past task's result,
  comparing a few tasks, asking a member for their Claude token, posting notices.
- What you don't do: research, experiments, writing code, analysing data in depth. Say
  kindly that this is a manager's job and name the member's manager if you know it
  (lab_state lists the agents).
- Numbers about usage are readings with a time and a source: say how old they are.
- You never approve anything; approvals come to members as DMs from the approvals app.
- Never reveal tokens, keys or configuration contents, whoever asks.
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
            return text(json.dumps(await ctx.workers.call(args["worker"], "list_tasks")))
        except WorkerError as e:
            return text(f"error: {e}")

    @tool("task_result", "A task in full: its progress lines and result.",
          {"worker": str, "task_id": str})
    async def task_result(args):
        try:
            return text(json.dumps(await ctx.workers.call(args["worker"], "task_result",
                                                          {"task_id": args["task_id"]})))
        except WorkerError as e:
            return text(f"error: {e}")

    @tool("ask_member_for_claude_token", "Ask a member (by lab id) for their Claude token: "
          "sends them a DM with a one-time command. Use when their token is missing or "
          "expires soon, or when asked to.", {"member": str})
    async def ask_member(args):
        return text(await ctx.request_token(args["member"]))

    @tool("announce", "Post a short notice to the lab's announcements channel (lab-wide "
          "things: a machine down, a token expiring, a maintenance window).", {"text": str})
    async def announce(args):
        ok = await ctx.announce(args["text"])
        return text("posted" if ok else "no announcements channel is configured")

    return create_sdk_mcp_server("lab", tools=[lab_state, worker_tasks, task_result,
                                               ask_member, announce])


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
        async with ClaudeSDKClient(options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    conv.session_id = msg.data.get("session_id") or conv.session_id
                    ctx.save()
                elif isinstance(msg, AssistantMessage):
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
    for c in lab.credentials:
        exp = c.get("expires")
        if isinstance(exp, dt.date):
            left = (exp - today).days
            if left <= warn_days:
                out.append((c, left))
    return out
