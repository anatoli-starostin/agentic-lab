"""The manager agent's lab tools.

Built per turn, bound to the conversation and to the member whose message the agent is
answering. That member is decided by code (from the Slack event), never by the LLM: it's
who a submitted task is for, and who is asked to approve anything it leads to.
"""
import base64
import os

import httpx2
from claude_agent_sdk import create_sdk_mcp_server, tool

from .workers import WorkerError

TOOL_NAMES = ("list_workers", "submit_task", "my_tasks", "task_status", "task_result",
              "cancel_task", "pending_approvals", "add_approval_note", "post_file",
              "post_worker_file", "send_file_to_worker", "recall", "remember")
LAB_TOOLS = tuple(f"mcp__lab__{n}" for n in TOOL_NAMES)


class BrokerClient:
    """The manager's side of the broker: register tasks, see and annotate pending requests."""

    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    async def _req(self, method, path, **kw):
        async with httpx2.AsyncClient(headers=self.headers, timeout=20) as c:
            r = await c.request(method, f"{self.url}{path}", **kw)
            if r.status_code >= 400:
                raise WorkerError(f"broker: {r.json().get('error', r.status_code)}")
            return r.json()

    async def register_task(self, worker, task_id, requester):
        return await self._req("POST", "/v1/tasks", json={
            "worker": worker, "task_id": task_id, "requester": requester})

    async def pending(self):
        return (await self._req("GET", "/v1/pending"))["pending"]

    async def add_context(self, rid, text):
        return await self._req("POST", f"/v1/requests/{rid}/context", json={"text": text})


def _text(s):
    return {"content": [{"type": "text", "text": s}]}


def _err(e):
    return {"content": [{"type": "text", "text": f"error: {e}"}], "is_error": True}


def visible(ctx, task):
    """May the current conversation see this tracked task? In a private conversation (DM):
    the member's own tasks. In a channel thread: the tasks submitted from that thread."""
    if ctx.private:
        return task.requester == ctx.requester
    return task.conversation == ctx.conv.key


def build(ctx):
    """`ctx` carries: workers (Workers), broker (BrokerClient or None), state
    (ManagerState), conv (Conversation), requester (human id), private (a DM?), memory
    (Memory), workdir, slack (poster with `upload(channel, thread_ts, path, title,
    comment)`), describe_workers() -> list."""

    @tool("list_workers", "The workers this manager can use, with their status and queue.", {})
    async def list_workers(args):
        return _text(str(await ctx.describe_workers()))

    @tool("submit_task", "Give a worker a task. Runs first come, first served. The task is "
          "for the member you're talking with. Returns the task id.",
          {"type": "object", "properties": {
              "worker": {"type": "string"}, "instruction": {"type": "string"},
              "project": {"type": "string"}},
           "required": ["worker", "instruction"]})
    async def submit_task(args):
        try:
            t = await ctx.workers.call(args["worker"], "submit_task", retries=0, args={
                "instruction": args["instruction"], "requester": ctx.requester,
                **({"project": args["project"]} if args.get("project") else {})})
            if ctx.broker is not None:
                await ctx.broker.register_task(args["worker"], t["id"], ctx.requester)
            ctx.state.track(t["id"], args["worker"], ctx.conv.key, ctx.requester)
        except (WorkerError, KeyError, TypeError) as e:
            return _err(e)
        return _text(f"Submitted {t['id']} to {args['worker']} (state: {t['state']}). Its "
                     f"progress and result will be posted in this conversation.")

    @tool("my_tasks", "The tasks you submitted that this conversation may see, with their "
          "last known state.", {})
    async def my_tasks(args):
        rows = [f"{t.task_id} on {t.worker} for {t.requester}: {t.state}"
                for t in ctx.state.tasks.values() if visible(ctx, t)]
        return _text("\n".join(rows) or "no tasks")

    async def _task(args, tool_name):
        t = ctx.state.tasks.get(args.get("task_id"))
        if t is None or not visible(ctx, t):
            return _err("no such task in this conversation (see my_tasks)")
        if t.worker != args.get("worker"):
            return _err(f"{t.task_id} runs on {t.worker}")
        try:
            return _text(str(await ctx.workers.call(args["worker"], tool_name,
                                                    {"task_id": args["task_id"]})))
        except WorkerError as e:
            return _err(e)

    task_schema = {"type": "object", "properties": {
        "worker": {"type": "string"}, "task_id": {"type": "string"}},
        "required": ["worker", "task_id"]}

    @tool("task_status", "A task's state and latest progress.", task_schema)
    async def task_status(args):
        return await _task(args, "task_status")

    @tool("task_result", "A task in full: progress and, when finished, its result.", task_schema)
    async def task_result(args):
        return await _task(args, "task_result")

    @tool("cancel_task", "Cancel a task (only when the member asks for it).", task_schema)
    async def cancel_task(args):
        return await _task(args, "cancel_task")

    @tool("pending_approvals", "Approval requests from your tasks that wait for a human. "
          "You can't decide them; you can add a note for the human.", {})
    async def pending_approvals(args):
        if ctx.broker is None:
            return _text("no approval broker is configured")
        try:
            pending = await ctx.broker.pending()
        except WorkerError as e:
            return _err(e)
        shown = []
        for r in pending:
            t = ctx.state.tasks.get(r.get("task_id") or "")
            if (t is not None and visible(ctx, t)) or (
                    t is None and ctx.private and r.get("human") == ctx.requester):
                shown.append({k: r[k] for k in ("id", "agent", "task_id", "human", "detail",
                                                "options", "expires", "context")})
        return _text(str(shown) if shown else "nothing pending")

    @tool("add_approval_note", "Add a note to a pending approval request, shown to the "
          "human with it (e.g. why the worker needs this). It does not approve anything.",
          {"request_id": str, "note": str})
    async def add_approval_note(args):
        if ctx.broker is None:
            return _text("no approval broker is configured")
        try:
            await ctx.broker.add_context(args["request_id"], args["note"])
        except WorkerError as e:
            return _err(e)
        return _text("note added")

    @tool("post_file", "Post a file from your working folder into this conversation "
          "(a plot, a report...).",
          {"type": "object", "properties": {"path": {"type": "string"},
                                            "comment": {"type": "string"}},
           "required": ["path"]})
    async def post_file(args):
        root = os.path.realpath(ctx.workdir)
        full = os.path.realpath(os.path.join(root, args["path"]))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return _err("only existing files in your working folder can be posted")
        await ctx.slack.upload(ctx.conv.channel, ctx.conv.thread_ts, full,
                               os.path.basename(full), args.get("comment"))
        return _text("posted")

    @tool("post_worker_file", "Fetch a file from a worker's working folder (e.g. a task's "
          "artifact) and post it into this conversation. Up to 20 MB.",
          {"type": "object", "properties": {"worker": {"type": "string"},
                                            "path": {"type": "string"},
                                            "comment": {"type": "string"}},
           "required": ["worker", "path"]})
    async def post_worker_file(args):
        try:
            f = await ctx.workers.call(args["worker"], "read_file", {"path": args["path"]})
        except WorkerError as e:
            return _err(e)
        inbox = os.path.join(ctx.workdir, "from-workers", args["worker"])
        os.makedirs(inbox, exist_ok=True)
        local = os.path.join(inbox, os.path.basename(f["name"]))
        with open(local, "wb") as out:
            out.write(base64.b64decode(f["base64"]))
        await ctx.slack.upload(ctx.conv.channel, ctx.conv.thread_ts, local, f["name"],
                               args.get("comment"))
        return _text(f"posted {f['name']} (a copy is in {local})")

    @tool("send_file_to_worker", "Send a file from your working folder (e.g. a member's "
          "attachment) to a worker, before submitting a task that needs it. Returns where it "
          "is on the worker: mention that path in the task's instruction. Up to 20 MB.",
          {"worker": str, "path": str})
    async def send_file_to_worker(args):
        root = os.path.realpath(ctx.workdir)
        full = os.path.realpath(os.path.join(root, args["path"]))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return _err("only existing files in your working folder can be sent")
        with open(full, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        try:
            r = await ctx.workers.call(args["worker"], "write_file",
                                       {"name": os.path.basename(full), "base64_data": data})
        except WorkerError as e:
            return _err(e)
        return _text(f"sent; on {args['worker']} it is at {r['path']}")

    @tool("recall", "What you remember about the member you're talking with (their projects, "
          "preferences, ongoing work). Empty in channel threads.", {})
    async def recall(args):
        return _text(ctx.memory.recall(ctx.requester, ctx.private))

    @tool("remember", "Remember a durable fact about the member or their work, for later "
          "conversations with them (not in channel threads).", {"note": str})
    async def remember(args):
        return _text(ctx.memory.remember(ctx.requester, ctx.private, args["note"]))

    return create_sdk_mcp_server("lab", tools=[
        list_workers, submit_task, my_tasks, task_status, task_result, cancel_task,
        pending_approvals, add_approval_note, post_file, post_worker_file,
        send_file_to_worker, recall, remember])
