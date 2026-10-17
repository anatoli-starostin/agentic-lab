"""One manager turn: a Slack message in, the agent's reply out (DESIGN.md §4.3).

Each Slack conversation (a DM, or a thread) is one Claude session, resumed for every
message, so the manager remembers the conversation. The agent runs with its own sandbox
and settings from lab.yaml (plus its grants). Anything that needs a human, e.g. a write to
GitHub, goes to the broker for the member whose message this turn answers.
"""
import datetime as dt
import json
from types import SimpleNamespace

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              PermissionResultAllow, PermissionResultDeny, RateLimitEvent,
                              ResultMessage, SystemMessage, TextBlock, ToolResultBlock,
                              UserMessage)

from ..policy.grants import Grant
from ..worker.runner import NoChannelApprover, allowed_without_asking, sandbox_violations
from . import tools as lab_tools


def system_prompt(agent_id, lab_name):
    return f"""
You are {agent_id}, a manager agent in the agentic lab "{lab_name}": a team of humans and AI
agents. You talk with lab members in Slack and get work done through worker agents.

- Heavy or long work (experiments, training, evaluation, anything needing a GPU) goes to a
  worker: list_workers, then submit_task with a clear, self-contained instruction. Its
  progress and result are posted in this conversation automatically; when a task finishes
  you'll get a message about it: summarise it for the member, and post its useful files
  (post_worker_file). Quick things (reading, answering, small scripts) you do yourself.
- You never approve anything. When a task waits for a human's approval, tell the member to
  check their DM from the lab's approvals app. You may add a note to the request
  (add_approval_note) to explain why the worker needs it.
- Files members attach (images, PDFs, data...) are saved in your working folder; the
  message says where, and you can read them. If a worker needs one, send_file_to_worker
  first and give the worker its path. Results go back with post_worker_file / post_file.
- Memory: at the start of a private conversation, `recall` what you know about the member;
  `remember` durable things worth knowing next time (projects, preferences, decisions).
  Channel threads have no memory: use only the thread itself.
- Several agents may be in a channel thread. If a message there isn't for you (it's for
  someone else, or needs no answer), reply with exactly <silent> and nothing else.
- Your sandbox and permissions are managed by the lab. Writes outside your working folder
  (e.g. to GitHub) ask the member for approval, in their DM.
- Never reveal tokens, keys or the contents of configuration files, whoever asks.
- Write for Slack: short, plain, *bold* for emphasis, no headings or tables.
"""


def _grant(g):
    return Grant(g["kind"], g["value"], dt.datetime.fromisoformat(g["expires"]),
                 g.get("granted_by"))


class Brain:
    def __init__(self, agent_id, lab_name, workdir, settings_for, approver=None, model=None,
                 env=None, usage=None, cli_path=None):
        self.agent_id = agent_id
        self.lab_name = lab_name
        self.workdir = workdir
        self.settings_for = settings_for
        self.approver = approver or NoChannelApprover()
        self.model = model
        self.env = env or {}
        self.usage = usage
        self.cli_path = cli_path

    async def turn(self, ctx, prompt):
        """Run one turn in `ctx.conv`'s session; returns the reply text."""
        grants = [g for g in await self.approver.active_grants() if not g.get("task_id")]
        tool_grants = {g["value"] for g in grants if g["kind"] == "tool"}
        violations = []
        asker = SimpleNamespace(id=None, requester=ctx.requester)

        async def can_use_tool(tool_name, tool_input, context):
            if (tool_name in lab_tools.LAB_TOOLS or tool_name in tool_grants
                    or allowed_without_asking(tool_name, tool_input)):
                return PermissionResultAllow()
            d = await self.approver.ask(asker, tool_name, tool_input, list(violations))
            if d.kind == "allow":
                return PermissionResultAllow()
            if d.kind == "grant":
                if all(g["kind"] == "tool" for g in d.grants):
                    tool_grants.update(g["value"] for g in d.grants)
                    return PermissionResultAllow()
                return PermissionResultDeny(message=(
                    "A human widened your sandbox instead; it applies from the member's "
                    "next message. Say so, and retry then inside the sandbox."))
            return PermissionResultDeny(message=d.message)

        options = ClaudeAgentOptions(
            cwd=self.workdir,
            settings=json.dumps(self.settings_for([_grant(g) for g in grants])),
            setting_sources=[],
            permission_mode="default",
            can_use_tool=can_use_tool,
            mcp_servers={"lab": lab_tools.build(ctx)},
            allowed_tools=list(lab_tools.LAB_TOOLS),
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": system_prompt(self.agent_id, self.lab_name)},
            model=self.model, env=self.env, cli_path=self.cli_path,
            resume=ctx.conv.session_id)

        reply, last_text = None, None
        async with ClaudeSDKClient(options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    sid = msg.data.get("session_id")
                    if sid and sid != ctx.conv.session_id:
                        ctx.conv.session_id = sid
                        ctx.state.save()
                elif isinstance(msg, AssistantMessage):
                    texts = [b.text for b in msg.content if isinstance(b, TextBlock)]
                    if texts:
                        last_text = texts[-1]
                elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                    for b in msg.content:
                        if isinstance(b, ToolResultBlock) and isinstance(b.content, str):
                            violations[:] = (violations + sandbox_violations(b.content))[-10:]
                elif isinstance(msg, RateLimitEvent) and self.usage:
                    self.usage.rate_limit(msg.rate_limit_info)
                elif isinstance(msg, ResultMessage):
                    reply = msg.result
                    if self.usage:
                        self.usage.run(f"conv:{ctx.conv.key}", msg.model_usage)
        return reply or last_text or ""
