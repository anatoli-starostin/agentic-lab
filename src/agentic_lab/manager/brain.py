"""One manager turn: a Slack message in, the agent's reply out (DESIGN.md §4.3).

Each Slack conversation (a DM, or a thread) is one Claude session, resumed for every
message, so the manager remembers the conversation. The agent runs with its own sandbox
and settings from lab.yaml (plus its grants). Anything that needs a human, e.g. a write to
GitHub, goes to the broker for the member whose message this turn answers.
"""
import asyncio
import datetime as dt
import json
import time
import warnings
from types import SimpleNamespace

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              PermissionResultAllow, PermissionResultDeny, RateLimitEvent,
                              ResultMessage, SystemMessage, TextBlock, ToolResultBlock,
                              ToolUseBlock, UserMessage)

from ..eventlog import TurnClock, tool_summary

from ..policy.confine import approved_input
from ..policy.grants import Grant

# The lab tools are auto-allowed on purpose (allowed_tools); the SDK warns that the
# permission callback then won't see them. That is the intent: silence it.
warnings.filterwarnings("ignore", message="can_use_tool will not be invoked")
from ..worker.runner import (NoChannelApprover, _grant_line, allowed_without_asking,
                             sandbox_violations)
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
  {budget_min} minutes, and nothing you start outlives your reply: there are no background
  helpers or background commands. Never say work is ongoing unless a worker task carries
  it. A task's progress and result are posted in this conversation automatically; when it
  finishes you'll get a message about it: summarise it for the member, and post only the
  file(s) they asked for (post_worker_file), in the form they asked for; name the rest
  (sources, notes, logs) and offer to post them. Never post files nobody asked for.
- Long jobs (training for hours) run as a worker's long runs: each one needs a human's
  approval (start_run), then the task is `waiting` (still in progress) until its runs end;
  it can take hours or days, and the worker may do other tasks meanwhile. Don't resubmit
  or chase it; when asked how it's going, task_status / task_result show its runs and the
  tail of their logs.
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
- Your Bash commands run in a sandbox: no internet except allowed hosts, writes only in
  your working folder and /tmp. Your sandbox and permissions are managed by the lab, not by
  you: don't suggest /sandbox, settings files or permission rules. Anything outside it
  (another host, a write elsewhere, e.g. to GitHub) asks the member for approval in their
  DM: retry the command outside the sandbox (dangerouslyDisableSandbox) and the lab asks
  them. When a member explicitly asks for something the sandbox refused (a download, an
  install), do exactly that: never tell them their approval can't help. They may run it
  once or widen your sandbox (then your session restarts with it, and you retry inside).
  A program may need several hosts, revealed one at a time: a refusal for another host
  means asking again, not giving up.
- No agent ever has root (sudo, apt): approval can't change that. Install into your own
  folders instead (a release binary, a venv or `pip --user` package), or ask for an admin.
  For a worker, send it a task: it asks for its own approvals the same way.
- Never reveal tokens, keys or the contents of configuration files, whoever asks.
- Write for Slack: short, plain, *bold* for emphasis, no headings or tables.
"""


def _grant(g):
    return Grant(g["kind"], g["value"], dt.datetime.fromisoformat(g["expires"]),
                 g.get("granted_by"))


MAX_RESTARTS = 4            # sandbox widenings applied within one turn
RECENT_VIOLATIONS_S = 1800  # sandbox refusals offered again in a later turn, for this long
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
# Each message is its own Claude Code process, which ends with the reply: a background
# helper or command started in it would die unseen while the member is told it's working,
# and its late notification would derail the conversation's next turn. So none can start.
NO_BACKGROUND_ENV = {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}
GIT_ENV = ("GIT_CONFIG_GLOBAL", "GH_CONFIG_DIR", "LAB_GITHUB_DIR", "GH_TOKEN")   # github.prepare


def guest_prompt(owner, guest):
    return (f"\nThis conversation is with {guest}, a lab member who is NOT your member "
            f"({owner}): {guest} is a guest. With a guest you only talk: answer questions, "
            "discuss science and ideas, explain things. You have no tools here: you can't run "
            "commands, read files, use GitHub, give workers tasks, or remember anything for "
            f"later. What's posted in the thread is fine to discuss; don't share {owner}'s "
            f"work or files beyond it. If {guest} asks for real work or new checks "
            "(experiments, code, tasks, looking something up), say kindly that they're a guest "
            f"here (you're {owner}'s manager) and suggest {guest}'s own manager, a shared one, "
            f"or asking {owner}. Missing tools are not a fault: never say they're broken.\n")


def guest_notice(owner, guest):
    """The first line of a guest's turn: in the message itself, not only the system prompt."""
    return (f"[lab] {guest} is a guest here (you are {owner}'s dedicated manager): talk only, "
            "no tools. Discuss anything visible in this thread; for new checks or work, say "
            f"they're a guest and point to their own manager or to {owner}.")


GUEST_TIME_UP = ("[lab] Time's up: this conversation is with a guest and your turn ran past "
                 "its limit. Answer in a few lines now, with what you have.")
CUT_OFF = ("I ran past my time limit on this and had to stop. Tell me how you'd like to "
           "continue (I can hand it to a worker).")


class Brain:
    def __init__(self, agent_id, lab_name, workdir, settings_for, approver=None, model=None,
                 env=None, usage=None, cli_path=None, events=None, prompt_extra="",
                 turn_budget_s=TURN_BUDGET_S, live=None):
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
        self.live = live                    # what's going on now, for the web UI (live.Live)
        self._recent_violations = {}        # conversation -> (when, sandbox refusals)

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
        live = self.live
        if live is not None:
            live.start(ctx.conv.key, member=ctx.requester,
                       guest=bool(getattr(ctx, "guest", False)) or None)
        try:
            return await self._turn(ctx, prompt, log, live)
        finally:
            if live is not None:
                live.end(ctx.conv.key)

    async def _turn(self, ctx, prompt, log, live):
        grants = [g for g in await self.approver.active_grants() if not g.get("task_id")]
        tool_grants = {g["value"] for g in grants if g["kind"] == "tool"}
        # what the sandbox refused lately in this conversation, also in earlier turns: a
        # member who asks to retry it outside the sandbox gets offered every host it needs
        at, recent = self._recent_violations.get(ctx.conv.key, (0, []))
        violations = list(recent) if time.monotonic() - at < RECENT_VIOLATIONS_S else []
        restart = {"grants": None}
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
            # with its message, as the worker logs it: a timeout reads differently from a no
            log("approval", f"decided: {d.kind}" + (f" ({d.message[:200]})" if d.message
                                                     else ""))
            if d.kind == "allow":           # as approved; started confined (policy/confine)
                return PermissionResultAllow(
                    updated_input=approved_input(tool_name, tool_input))
            if d.kind == "grant":
                if all(g["kind"] == "tool" for g in d.grants):
                    tool_grants.update(g["value"] for g in d.grants)
                    return PermissionResultAllow()
                grants.extend(g for g in d.grants
                              if (g["kind"], g["value"]) not in {(x["kind"], x["value"])
                                                                 for x in grants})
                tool_grants.update(g["value"] for g in d.grants if g["kind"] == "tool")
                restart["grants"] = d.grants
                return PermissionResultDeny(interrupt=True, message=(
                    "Granted: " + "; ".join(_grant_line(g) for g in d.grants) +
                    ". Your session restarts now with the wider sandbox."))
            return PermissionResultDeny(message=d.message)

        def options():
            return ClaudeAgentOptions(
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
                                  if guest else (self.prompt_extra() if callable(self.prompt_extra)
                                                 else self.prompt_extra))},
                model=self.model, cli_path=self.cli_path,
                env={**{k: v for k, v in self.env.items() if not (guest and k in GIT_ENV)},
                     **NO_BACKGROUND_ENV},
                resume=ctx.conv.session_id)

        out = {"reply": None, "last": None}

        async def drain(client):
            async for msg in client.receive_response():
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    clock.started()
                    sid = msg.data.get("session_id")
                    if sid and sid != ctx.conv.session_id:
                        ctx.conv.session_id = sid
                        ctx.state.save()
                elif isinstance(msg, AssistantMessage):
                    clock.output(any(isinstance(b, TextBlock) for b in msg.content))
                    if live is not None:
                        live.assistant(ctx.conv.key, msg, tool_summary)
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            out["last"] = b.text
                        elif isinstance(b, ToolUseBlock):
                            log("tool", tool_summary(b.name, b.input))
                elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                    for b in msg.content:
                        if isinstance(b, ToolResultBlock) and live is not None:
                            live.tool_done(ctx.conv.key, b.tool_use_id)
                        if isinstance(b, ToolResultBlock) and isinstance(b.content, str):
                            violations[:] = (violations + sandbox_violations(b.content))[-10:]
                            log("result", b.content[:1000], error=bool(b.is_error) or None)
                elif isinstance(msg, RateLimitEvent) and self.usage:
                    self.usage.rate_limit(msg.rate_limit_info)
                elif isinstance(msg, ResultMessage):
                    out["reply"] = msg.result
                    if self.usage:
                        self.usage.run(f"conv:{ctx.conv.key}", msg.model_usage)

        deadline = asyncio.get_running_loop().time() + budget
        query = prompt
        # a Claude Code start-up per message (#69): measured, however the turn ends
        async with TurnClock(log) as clock:
            for _ in range(MAX_RESTARTS + 1):
                restart["grants"] = None
                async with self.client_factory(options()) as client:
                    await client.query(query)
                    try:
                        left = deadline - asyncio.get_running_loop().time()
                        await asyncio.wait_for(drain(client), max(left, 0.01))
                    except asyncio.TimeoutError:
                        # too long: stop it, so the conversation is free again, and let the
                        # agent say where it got to (or hand the rest to a worker) in one
                        # short turn
                        log("note", f"turn cut off after {budget:.0f} s")
                        clock.cut = True
                        await self._stop(client, drain)
                        out["reply"] = None
                        await client.query(GUEST_TIME_UP if guest else TIME_UP)
                        try:
                            await asyncio.wait_for(drain(client), WRAP_UP_S)
                        except asyncio.TimeoutError:
                            await self._stop(client, drain)
                            out["reply"] = out["last"] = CUT_OFF
                        break
                if not restart["grants"]:
                    break
                # a human widened the sandbox: a running session doesn't pick up new
                # settings, so the session goes on in a new process that has them, in this
                # same turn (the member needn't send another message)
                log("note", "sandbox widened: session restarted with it")
                out["reply"] = None
                query = ("[lab] Instead of running your last action outside the sandbox, a "
                         "human widened your sandbox: " + "; ".join(
                             _grant_line(g) for g in restart["grants"]) + ". (Your session "
                         "was restarted to apply it; that's why the action showed as not "
                         "run.) Retry what you were doing, inside the sandbox (without "
                         "dangerouslyDisableSandbox). If it's then refused for another host, "
                         "ask again: retry outside the sandbox.")
        self._recent_violations[ctx.conv.key] = (time.monotonic(), list(violations))
        reply, last_text = out["reply"], out["last"]
        log("reply", reply or last_text or "")
        return reply or last_text or ""
