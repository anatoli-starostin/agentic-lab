"""Runs one task through Claude (DESIGN.md §10).

`SdkRunner` drives Claude Code through the Agent SDK, headless:
  - the agent's settings come from `lab.yaml` (the sandbox model) plus its active grants;
  - no user/project settings are loaded (a repo's own `.claude/` can't add hooks);
  - the agent reports through two lab tools, `report_progress` and `finish_task`;
  - anything that needs a human reaches `can_use_tool`, which asks the approver (the
    broker, DESIGN.md §6.3). What the sandbox refused just before is sent along, so the
    human can be offered the right grant.
  - a grant of a tool applies at once; a grant that widens the sandbox (a domain, a
    writable folder) restarts the session with the new settings, and the agent resumes
    where it was (a running session doesn't pick up new sandbox settings).
  - a task's session id is saved, so after a restart the task resumes its session.

`AgentRunner` is the interface the worker service uses, so a different runner (or a fake,
in tests) can stand in.
"""
import asyncio
import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              PermissionResultAllow, PermissionResultDeny, RateLimitEvent,
                              ResultMessage, SystemMessage, TextBlock, ToolResultBlock,
                              UserMessage, create_sdk_mcp_server, tool)

from ..policy.grants import Grant
from . import tasks as T

CANCEL_POLL_S = 2.0
MAX_SESSION_RESTARTS = 8        # grants applied within one task

LAB_TOOLS = ("mcp__lab__report_progress", "mcp__lab__finish_task")
_VIOLATIONS = re.compile(r"<sandbox_violations>(.*?)</sandbox_violations>", re.S)


def allowed_without_asking(tool_name, tool_input):
    """Calls our permission callback allows on its own.

    Claude Code sends some sandboxed Bash commands for approval anyway: `export X=...`, an
    env prefix (`FOO=bar cmd`), some `python3 -c` one-liners, loops over variables. Our
    sandbox confines every command that runs in it (verified on a worker host: writes
    outside, unlisted hosts and the systemd bus all stay refused), so a Bash command that
    stays inside the sandbox needs no human. Only one asking to run OUTSIDE it
    (`dangerouslyDisableSandbox`) does.
    """
    if tool_name in LAB_TOOLS:
        return True
    return tool_name == "Bash" and not tool_input.get("dangerouslyDisableSandbox")


def sandbox_violations(text):
    """The lines of `<sandbox_violations>` blocks in a tool result."""
    out = []
    for block in _VIOLATIONS.findall(text or ""):
        out += [ln.strip() for ln in block.splitlines() if ln.strip()]
    return out


@dataclass
class Decision:
    kind: str                     # "allow" | "grant" | "deny"
    message: str = ""
    grants: list = field(default_factory=list)   # for "grant": [{"kind", "value", ...}]


class Approver(Protocol):
    async def ask(self, task: T.Task, tool_name: str, tool_input: dict,
                  violations: list) -> Decision: ...

    async def active_grants(self) -> list[dict]: ...


class NoChannelApprover:
    """No approval channel: every request outside the sandbox is denied, with guidance."""

    async def ask(self, task, tool_name, tool_input, violations):
        return Decision("deny", (
            "No human approval channel is connected, so actions outside the sandbox are "
            "denied. Find a way to do this inside the sandbox (your working folder, /tmp, "
            "allowed hosts), or report in finish_task what you would need."))

    async def active_grants(self):
        return []


class AgentRunner(Protocol):
    async def run(self, task: T.Task, store: T.TaskStore) -> None:
        """Run `task` to a terminal state, recording progress and the result in `store`."""


def _system_prompt(agent_id, task):
    return f"""
You are {agent_id}, a worker agent in an agentic lab: a team of humans and AI agents.
This task comes from the human {task.requester}, through the manager agent {task.manager}.

How you work here:
- Your Bash commands run in a sandbox: no internet except allowed hosts, the lab's tailnet,
  writes only in your working folder and /tmp. Inside it you never need to ask.
- If the sandbox refuses something, the error says why. Prefer a way inside the sandbox.
  Running something outside it, or writing elsewhere, needs a human's permission: the lab
  asks the human (in Slack), and it may be denied or time out. If such an action runs,
  a human approved it. A human may also widen your sandbox for a while instead.
- Your sandbox and permissions are managed by the lab, not by you: don't suggest
  /sandbox, settings files or permission rules. To get more access, ask through the
  lab (retry outside the sandbox) or say what you need in finish_task.
- Report progress with the report_progress tool at meaningful steps (a few per task, not
  every command).
- When the task is done (or can't be done), call finish_task exactly once: a summary for
  the human, and the artifacts (file paths, commit ids, W&B run URLs) worth keeping.
"""


def _grant_line(g):
    what = {"domain": "access to {}", "write": "writes to {}", "tool": "the {} tool"}
    until = g.get("expires", "")[:16].replace("T", " ")
    return what.get(g["kind"], "{}").format(g["value"]) + (f" until {until} UTC" if until else "")


class SdkRunner:
    def __init__(self, agent_id, workdir, settings_for: Callable[[list], dict],
                 approver: Approver = None, model=None, env=None, cli_path=None, usage=None):
        """`settings_for(grants)` returns the agent's settings given its active Grants."""
        self.agent_id = agent_id
        self.workdir = workdir
        self.settings_for = settings_for
        self.approver = approver or NoChannelApprover()
        self.model = model
        self.env = env or {}
        self.cli_path = cli_path
        self.usage = usage          # a UsageLog, or None

    def _lab_server(self, task, store, outcome):
        @tool("report_progress", "Report progress on the current task to the humans "
              "following it.", {"message": str})
        async def report_progress(args):
            store.add_progress(task.id, args["message"])
            return {"content": [{"type": "text", "text": "reported"}]}

        @tool("finish_task", "Finish the current task: a summary for the human, and the "
              "artifacts worth keeping (paths, commits, run URLs).",
              {"type": "object",
               "properties": {"summary": {"type": "string"},
                              "artifacts": {"type": "array", "items": {"type": "string"}},
                              "success": {"type": "boolean"}},
               "required": ["summary"]})
        async def finish_task(args):
            outcome["result"] = {"summary": args["summary"],
                                 "artifacts": list(args.get("artifacts") or [])}
            outcome["success"] = bool(args.get("success", True))
            return {"content": [{"type": "text", "text": "task finished; stop here"}]}

        return create_sdk_mcp_server("lab", tools=[report_progress, finish_task])

    async def _grants_for(self, task):
        """Active grants that apply to this task (task-bound grants of other tasks don't)."""
        try:
            gs = await self.approver.active_grants()
        except Exception:
            gs = []
        return [g for g in gs if g.get("task_id") in (None, task.id)]

    def _options(self, task, store, outcome, session, grants):
        tool_grants = {g["value"] for g in grants if g["kind"] == "tool"}

        async def can_use_tool(tool_name, tool_input, context):
            if allowed_without_asking(tool_name, tool_input) or tool_name in tool_grants:
                return PermissionResultAllow()
            if store.get(task.id).cancel_requested:
                return PermissionResultDeny(message="The task was cancelled.", interrupt=True)
            store.update(task.id, state=T.AWAITING)
            try:
                d = await self.approver.ask(task, tool_name, tool_input,
                                            list(session["violations"]))
            finally:
                if store.get(task.id).state == T.AWAITING:
                    store.update(task.id, state=T.RUNNING)
            if d.kind == "allow":
                return PermissionResultAllow()
            if d.kind == "grant":
                if all(g["kind"] == "tool" for g in d.grants):
                    tool_grants.update(g["value"] for g in d.grants)
                    return PermissionResultAllow()
                session["restart_for"] = d.grants
                return PermissionResultDeny(interrupt=True, message=(
                    "Granted: " + "; ".join(_grant_line(g) for g in d.grants) +
                    ". Your session restarts now with the wider sandbox."))
            return PermissionResultDeny(message=d.message)

        settings = self.settings_for([Grant(g["kind"], g["value"],
                                            dt.datetime.fromisoformat(g["expires"]),
                                            g.get("granted_by")) for g in grants])
        return ClaudeAgentOptions(
            cwd=self.workdir,
            settings=json.dumps(settings),
            setting_sources=[],
            permission_mode="default",
            can_use_tool=can_use_tool,
            mcp_servers={"lab": self._lab_server(task, store, outcome)},
            allowed_tools=list(LAB_TOOLS),
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": _system_prompt(self.agent_id, task)},
            model=self.model,
            env=self.env,
            cli_path=self.cli_path,
            resume=store.get(task.id).session_id,
        )

    async def run(self, task, store):
        outcome = {}
        prompt = task.instruction if task.session_id is None else (
            "The worker restarted while you were on this task. Continue it from where you "
            "were; check the state of your working folder first.")
        result_msg, last_text = None, None
        for _ in range(MAX_SESSION_RESTARTS + 1):
            session = {"violations": [], "restart_for": None}
            grants = await self._grants_for(task)
            try:
                result_msg, last_text = await self._session(
                    task, store, outcome, session, grants, prompt)
            except Exception as e:   # the agent process failed: record it, keep the worker up
                store.update(task.id, state=T.FAILED, error=f"{type(e).__name__}: {e}")
                return
            if session["restart_for"] and "result" not in outcome \
                    and not store.get(task.id).cancel_requested:
                prompt = ("Instead of running your last action outside the sandbox, a human "
                          "widened your sandbox: " +
                          "; ".join(_grant_line(g) for g in session["restart_for"]) +
                          ". (Your session was restarted to apply it; that's why the action "
                          "showed as not run.) Retry what you were doing, inside the sandbox "
                          "(without dangerouslyDisableSandbox).")
                continue
            break
        self._finish(task, store, outcome, result_msg, last_text)

    async def _session(self, task, store, outcome, session, grants, prompt):
        last_text, result_msg = None, None
        async with ClaudeSDKClient(self._options(task, store, outcome, session, grants)) as client:
            watcher = asyncio.create_task(self._watch_cancel(task, store, client))
            try:
                await client.query(prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, SystemMessage) and msg.subtype == "init":
                        sid = msg.data.get("session_id")
                        if sid and sid != store.get(task.id).session_id:
                            store.update(task.id, session_id=sid)
                    elif isinstance(msg, AssistantMessage):
                        texts = [b.text for b in msg.content if isinstance(b, TextBlock)]
                        if texts:
                            last_text = texts[-1]
                    elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                        for b in msg.content:
                            if isinstance(b, ToolResultBlock):
                                text = b.content if isinstance(b.content, str) else " ".join(
                                    x.get("text", "") for x in (b.content or [])
                                    if isinstance(x, dict))
                                session["violations"] = (session["violations"] +
                                                         sandbox_violations(text))[-10:]
                    elif isinstance(msg, RateLimitEvent) and self.usage:
                        self.usage.rate_limit(msg.rate_limit_info)
                    elif isinstance(msg, ResultMessage):
                        result_msg = msg
                        if self.usage:
                            self.usage.run(task.id, msg.model_usage)
                            if msg.is_error and "limit" in (msg.result or "").lower():
                                self.usage.limit_hit(msg.result)
            finally:
                watcher.cancel()
        return result_msg, last_text

    def _finish(self, task, store, outcome, result_msg, last_text):
        if store.get(task.id).cancel_requested:
            store.update(task.id, state=T.CANCELLED)
        elif "result" in outcome:
            state = T.DONE if outcome.get("success", True) else T.FAILED
            store.update(task.id, state=state, result=outcome["result"])
        elif result_msg is not None and not result_msg.is_error:
            # the agent stopped without finish_task: keep its last words as the summary
            store.update(task.id, state=T.DONE,
                         result={"summary": result_msg.result or last_text or "", "artifacts": []})
        else:
            err = "; ".join(result_msg.errors or []) if result_msg else "no result"
            store.update(task.id, state=T.FAILED,
                         error=f"agent ended with an error: {err or result_msg.subtype}")

    async def _watch_cancel(self, task, store, client):
        while True:
            await asyncio.sleep(CANCEL_POLL_S)
            if store.get(task.id).cancel_requested:
                await client.interrupt()
                return
