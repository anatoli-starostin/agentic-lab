"""One manager turn: a Slack message in, the agent's reply out (DESIGN.md §4.3).

Each Slack conversation (a DM, or a thread) is one Claude session, resumed for every
message, so the manager remembers the conversation. The agent runs with its own sandbox
and settings from lab.yaml (plus its grants). Anything that needs a human, e.g. a write to
GitHub, goes to the broker for the member whose message this turn answers.
"""
import asyncio
import datetime as dt
import json
import warnings
from types import SimpleNamespace

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              PermissionResultAllow, PermissionResultDeny, RateLimitEvent,
                              ResultMessage, SystemMessage, TextBlock, ToolResultBlock,
                              ToolUseBlock, UserMessage)

from ..eventlog import tool_summary

from ..policy.grants import Grant

# The lab tools are auto-allowed on purpose (allowed_tools); the SDK warns that the
# permission callback then won't see them. That is the intent: silence it.
warnings.filterwarnings("ignore", message="can_use_tool will not be invoked")
from ..worker.runner import NoChannelApprover, allowed_without_asking, sandbox_violations
from . import tools as lab_tools


def system_prompt(agent_id, lab_name, budget_min=5):
    return f"""
You are {agent_id}, a manager agent in the agentic lab "{lab_name}": a team of humans and AI
agents. You talk with lab members in Slack and get work done through worker agents.

- Stay reachable: members must always be able to talk to you. Do work yourself only when
  it takes a few minutes at most: reading, answering, a GitHub issue or comment, a small
  script or a small code fix. Anything longer, and anything needing a GPU (experiments,
  training, evaluation, bigger code changes), goes to a worker: list_workers, then
  submit_task with a clear, self-contained instruction. Your turns are cut off after
  {budget_min} minutes. A task's progress and result are posted in this conversation
  automatically; when it finishes you'll get a message about it: summarise it for the
  member, and post its useful files (post_worker_file).
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


TURN_BUDGET_S = 300     # a manager's turn is cut off after this: it must stay reachable
WRAP_UP_S = 90          # ...and then gets this long to say where it got to
TIME_UP = ("[lab] Time's up: your turn ran past its time limit and was stopped, so the "
           "member can reach you. Don't continue the work here. In a few lines: say where you "
           "got to, and either submit the rest to a worker (submit_task) or tell the member "
           "what's left.")
GUEST_TURN_BUDGET_S = 120   # a guest's turn (someone else's dedicated manager): talk only
# every built-in tool, removed from a guest's turn (on top of the permission callback)
BUILTIN_TOOLS = ["Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "MultiEdit",
                 "Glob", "Grep", "NotebookEdit", "WebFetch", "WebSearch", "Task",
                 "TodoWrite", "SlashCommand", "ExitPlanMode"]
GIT_ENV = ("GH_TOKEN", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")


def guest_prompt(owner, guest):
    return (f"\nThis conversation is with {guest}, a lab member who is NOT your member "
            f"({owner}): {guest} is a guest. With a guest you only talk: answer questions, "
            "discuss science and ideas, explain things. You have no tools here: you can't run "
            "commands, read files, use GitHub, give workers tasks, or remember anything for "
            f"later; don't share {owner}'s work or files. If {guest} asks for real work "
            "(experiments, code, tasks, anything long), say kindly that you're "
            f"{owner}'s manager and suggest {guest}'s own manager or a shared one.\n")


GUEST_TIME_UP = ("[lab] Time's up: this conversation is with a guest and your turn ran past "
                 "its limit. Answer in a few lines now, with what you have.")
CUT_OFF = ("I ran past my time limit on this and had to stop. Tell me how you'd like to "
           "continue (I can hand it to a worker).")


class Brain:
    def __init__(self, agent_id, lab_name, workdir, settings_for, approver=None, model=None,
                 env=None, usage=None, cli_path=None, events=None, prompt_extra="",
                 turn_budget_s=TURN_BUDGET_S):
        self.agent_id = agent_id
        self.lab_name = lab_name
        self.workdir = workdir
        self.settings_for = settings_for
        self.approver = approver or NoChannelApprover()
        self.model = model
        self.env = env or {}
        self.usage = usage
        self.cli_path = cli_path
        self.events = events
        self.prompt_extra = prompt_extra    # e.g. its GitHub identity
        self.turn_budget_s = turn_budget_s  # a turn longer than this is cut off
        self.client_factory = ClaudeSDKClient   # tests pass a fake

    @staticmethod
    async def _stop(client, drain):
        """Interrupt the running turn and read what's left of it."""
        try:
            await client.interrupt()
            await asyncio.wait_for(drain(client), 30)
        except (asyncio.TimeoutError, Exception):
            pass

    async def turn(self, ctx, prompt):
        """Run one turn in `ctx.conv`'s session; returns the reply text."""
        log = (lambda kind, text="", **m: self.events.add(kind, text, conv=ctx.conv.key,
                                                          member=ctx.requester, **m)
               ) if self.events is not None else (lambda *a, **k: None)
        log("message", prompt)
        grants = [g for g in await self.approver.active_grants() if not g.get("task_id")]
        tool_grants = {g["value"] for g in grants if g["kind"] == "tool"}
        violations = []
        asker = SimpleNamespace(id=None, requester=ctx.requester)
        guest = bool(getattr(ctx, "guest", False))    # someone else's dedicated manager: talk only
        budget = GUEST_TURN_BUDGET_S if guest else self.turn_budget_s

        async def can_use_tool(tool_name, tool_input, context):
            if guest:
                return PermissionResultDeny(message=(
                    "This conversation is with a guest: no tools. Answer from what you know, "
                    "or suggest their own manager for real work."))
            if (tool_name in lab_tools.LAB_TOOLS or tool_name in tool_grants
                    or allowed_without_asking(tool_name, tool_input)):
                return PermissionResultAllow()
            log("approval", "asked: " + tool_summary(tool_name, tool_input))
            d = await self.approver.ask(asker, tool_name, tool_input, list(violations))
            log("approval", f"decided: {d.kind}")
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
            mcp_servers={} if guest else {"lab": lab_tools.build(ctx)},
            allowed_tools=[] if guest else list(lab_tools.LAB_TOOLS),
            disallowed_tools=BUILTIN_TOOLS if guest else [],
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": system_prompt(self.agent_id, self.lab_name,
                                                   max(1, round(budget / 60)))
                           + (guest_prompt(getattr(ctx, "owner_name", "your member"),
                                           getattr(ctx, "guest_name", ctx.requester))
                              if guest else self.prompt_extra)},
            model=self.model, cli_path=self.cli_path,
            env={k: v for k, v in self.env.items() if not (guest and k in GIT_ENV)},
            resume=ctx.conv.session_id)

        out = {"reply": None, "last": None}

        async def drain(client):
            async for msg in client.receive_response():
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    sid = msg.data.get("session_id")
                    if sid and sid != ctx.conv.session_id:
                        ctx.conv.session_id = sid
                        ctx.state.save()
                elif isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            out["last"] = b.text
                        elif isinstance(b, ToolUseBlock):
                            log("tool", tool_summary(b.name, b.input))
                elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                    for b in msg.content:
                        if isinstance(b, ToolResultBlock) and isinstance(b.content, str):
                            violations[:] = (violations + sandbox_violations(b.content))[-10:]
                            log("result", b.content[:1000], error=bool(b.is_error) or None)
                elif isinstance(msg, RateLimitEvent) and self.usage:
                    self.usage.rate_limit(msg.rate_limit_info)
                elif isinstance(msg, ResultMessage):
                    out["reply"] = msg.result
                    if self.usage:
                        self.usage.run(f"conv:{ctx.conv.key}", msg.model_usage)

        async with self.client_factory(options) as client:
            await client.query(prompt)
            try:
                await asyncio.wait_for(drain(client), budget)
            except asyncio.TimeoutError:
                # too long: stop it, so the conversation is free again, and let the agent say
                # where it got to (or hand the rest to a worker) in one short turn
                log("note", f"turn cut off after {budget:.0f} s")
                await self._stop(client, drain)
                out["reply"] = None
                await client.query(GUEST_TIME_UP if guest else TIME_UP)
                try:
                    await asyncio.wait_for(drain(client), WRAP_UP_S)
                except asyncio.TimeoutError:
                    await self._stop(client, drain)
                    out["reply"] = out["last"] = CUT_OFF
        reply, last_text = out["reply"], out["last"]
        log("reply", reply or last_text or "")
        return reply or last_text or ""
