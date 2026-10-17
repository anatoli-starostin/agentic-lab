"""Runs one task through Claude (DESIGN.md §10).

`SdkRunner` drives Claude Code through the Agent SDK, headless:
  - the agent's settings come from `lab.yaml` (the sandbox model, its grants);
  - no user/project settings are loaded (a repo's own `.claude/` can't add hooks);
  - the agent reports through two lab tools, `report_progress` and `finish_task`;
  - anything the sandbox doesn't auto-allow reaches `can_use_tool`, which asks the
    approver. Until the broker exists (M2), the approver denies, telling the agent to find
    a way inside the sandbox.
  - a task's session id is saved, so after a restart the task resumes its session.

`AgentRunner` is the interface the worker service uses, so a different runner (or a fake,
in tests) can stand in.
"""
import asyncio
import json
from typing import Protocol

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              PermissionResultAllow, PermissionResultDeny, ResultMessage,
                              SystemMessage, TextBlock, create_sdk_mcp_server, tool)

from . import tasks as T

CANCEL_POLL_S = 2.0

LAB_TOOLS = ("mcp__lab__report_progress", "mcp__lab__finish_task")


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


class AgentRunner(Protocol):
    async def run(self, task: T.Task, store: T.TaskStore) -> None:
        """Run `task` to a terminal state, recording progress and the result in `store`."""


class Approver(Protocol):
    async def decide(self, task: T.Task, tool_name: str, tool_input: dict, context):
        """Return PermissionResultAllow or PermissionResultDeny."""


class NoChannelApprover:
    """M1: there is no approval channel yet, so every request outside the sandbox is
    denied, and the agent is told why."""

    async def decide(self, task, tool_name, tool_input, context):
        return PermissionResultDeny(message=(
            "No human approval channel is connected yet, so actions outside the sandbox "
            "are denied. Find a way to do this inside the sandbox (your working folder, "
            "/tmp, allowed hosts), or report in finish_task what you would need."))


def _system_prompt(agent_id, task):
    return f"""
You are {agent_id}, a worker agent in an agentic lab: a team of humans and AI agents.
This task comes from the human {task.requester}, through the manager agent {task.manager}.

How you work here:
- Your Bash commands run in a sandbox: no internet except allowed hosts, the lab's tailnet,
  writes only in your working folder and /tmp. Inside it you never need to ask.
- If the sandbox refuses something, the error says why. Prefer a way inside the sandbox.
  Running something outside it, or writing elsewhere, needs a human's permission, which
  may be denied or time out.
- Report progress with the report_progress tool at meaningful steps (a few per task, not
  every command).
- When the task is done (or can't be done), call finish_task exactly once: a summary for
  the human, and the artifacts (file paths, commit ids, W&B run URLs) worth keeping.
"""


class SdkRunner:
    def __init__(self, agent_id, workdir, settings: dict, approver: Approver = None,
                 model=None, env=None, cli_path=None):
        self.agent_id = agent_id
        self.workdir = workdir
        self.settings = settings
        self.approver = approver or NoChannelApprover()
        self.model = model
        self.env = env or {}
        self.cli_path = cli_path

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

    def _options(self, task, store, outcome):
        async def can_use_tool(tool_name, tool_input, context):
            if allowed_without_asking(tool_name, tool_input):
                return PermissionResultAllow()
            if store.get(task.id).cancel_requested:
                return PermissionResultDeny(message="The task was cancelled.", interrupt=True)
            store.update(task.id, state=T.AWAITING)
            try:
                return await self.approver.decide(task, tool_name, tool_input, context)
            finally:
                if store.get(task.id).state == T.AWAITING:
                    store.update(task.id, state=T.RUNNING)

        return ClaudeAgentOptions(
            cwd=self.workdir,
            settings=json.dumps(self.settings),
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
            resume=task.session_id,
        )

    async def run(self, task, store):
        outcome = {}
        prompt = task.instruction if task.session_id is None else (
            "The worker restarted while you were on this task. Continue it from where you "
            "were; check the state of your working folder first.")
        last_text = None
        result_msg = None
        try:
            async with ClaudeSDKClient(self._options(task, store, outcome)) as client:
                watcher = asyncio.create_task(self._watch_cancel(task, store, client))
                try:
                    await client.query(prompt)
                    async for msg in client.receive_response():
                        if isinstance(msg, SystemMessage) and msg.subtype == "init":
                            sid = msg.data.get("session_id")
                            if sid and sid != task.session_id:
                                store.update(task.id, session_id=sid)
                        elif isinstance(msg, AssistantMessage):
                            texts = [b.text for b in msg.content if isinstance(b, TextBlock)]
                            if texts:
                                last_text = texts[-1]
                        elif isinstance(msg, ResultMessage):
                            result_msg = msg
                finally:
                    watcher.cancel()
        except Exception as e:     # the agent process failed: record it, keep the worker up
            store.update(task.id, state=T.FAILED, error=f"{type(e).__name__}: {e}")
            return

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
