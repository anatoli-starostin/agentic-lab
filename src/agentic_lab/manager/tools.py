"""The manager agent's lab tools.

Built per turn, bound to the conversation and to the member whose message the agent is
answering. That member is decided by code (from the Slack event), never by the LLM: it's
who a submitted task is for, and who is asked to approve anything it leads to.
"""
import base64
import logging
import os

import httpx2
from claude_agent_sdk import create_sdk_mcp_server, tool

from .. import safefs
from .workers import WorkerError

TOOL_NAMES = ("list_workers", "submit_task", "my_tasks", "task_status", "task_result",
              "cancel_task", "pending_approvals", "add_approval_note", "post_file",
              "post_worker_file", "send_file_to_worker", "recall", "remember")
LAB_TOOLS = tuple(f"mcp__lab__{n}" for n in TOOL_NAMES)

log = logging.getLogger("agentic_lab.manager")


class BrokerClient:
    """The manager's side of the broker: register tasks, see and annotate pending requests."""

    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    async def _req(self, method, path, **kw):
        async with httpx2.AsyncClient(headers=self.headers, timeout=20) as c:
            r = await c.request(method, f"{self.url}{path}", **kw)
            if r.status_code >= 400:
                try:
                    msg = r.json().get("error", r.status_code)
                except ValueError:
                    msg = r.status_code
                e = WorkerError(f"broker: {msg}")
                e.status = r.status_code
                raise e
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


def _pending_visible(ctx, r):
    """A pending approval request this conversation may see: of a task it may see, or the
    member's own request in their DM (#62)."""
    t = ctx.state.tasks.get(r.get("task_id") or "")
    return (t is not None and visible(ctx, t)) or (
        t is None and ctx.private and r.get("human") == ctx.requester)


def _file_refused(ctx, worker, path):
    """Why this conversation may not fetch `path` from `worker` (None if it may). A shared
    worker holds several members' work: a member gets only their own tasks' folders,
    tasks/<id>/ (#62); admins (or `visibility: team`) anything."""
    lab = getattr(ctx, "lab", None)
    if lab is None or (lab.agents.get(worker) or {}).get("access") != "shared":
        return None
    h = lab.humans.get(ctx.requester) or {}
    if h.get("admin") or lab.data.get("visibility") == "team":
        return None
    parts = os.path.normpath(path).split(os.sep)
    if os.path.isabs(path) or len(parts) < 3 or parts[0] != "tasks" or ".." in parts:
        return (f"{worker} is shared: only files in tasks/<task id>/ of your own tasks can "
                "be fetched from it")
    t = ctx.state.tasks.get(parts[1])
    if t is None or t.worker != worker or not visible(ctx, t):
        return f"task {parts[1]} isn't one of this conversation's tasks on {worker}"
    return None


def _read_own(ctx, path, limit=20 * 1024 * 1024):
    """A plain file of this manager's working folder (never through a link: safefs), or
    None."""
    try:
        return safefs.read(os.path.realpath(ctx.workdir), path, limit=limit)
    except OSError:
        return None


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
            tid, state = t["id"], t["state"]
        except (WorkerError, KeyError, TypeError) as e:
            return _err(e)
        # followed from the moment the worker has it, whatever happens next (#58)
        ctx.state.track(tid, args["worker"], ctx.conv.key, ctx.requester,
                        registered=ctx.broker is None)
        note = ""
        if ctx.broker is not None:
            try:
                await ctx.broker.register_task(args["worker"], tid, ctx.requester)
                ctx.state.tasks[tid].registered = True
                ctx.state.save()
            except Exception as e:      # the follow-up retries it; never resubmit
                log.warning("registering %s with the broker: %s", tid, e)
                note = (" (Routing its approvals to the member is still pending; it's retried "
                        "automatically. Don't submit it again.)")
        return _text(f"Submitted {tid} to {args['worker']} (state: {state}). Its progress "
                     f"and result will be posted in this conversation.{note}")

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
        shown = [{k: r[k] for k in ("id", "agent", "task_id", "human", "detail", "options",
                                    "expires", "context")}
                 for r in pending if _pending_visible(ctx, r)]
        return _text(str(shown) if shown else "nothing pending")

    @tool("add_approval_note", "Add a note to a pending approval request, shown to the "
          "human with it (e.g. why the worker needs this). It does not approve anything.",
          {"request_id": str, "note": str})
    async def add_approval_note(args):
        if ctx.broker is None:
            return _text("no approval broker is configured")
        try:
            # only a request this conversation may see (a shared manager: the member's own)
            if not any(r.get("id") == args["request_id"] and _pending_visible(ctx, r)
                       for r in await ctx.broker.pending()):
                return _err(f"no pending request {args['request_id']} here")
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
        data = _read_own(ctx, args["path"])
        if data is None:
            return _err("only existing plain files in your working folder can be posted")
        name = os.path.basename(os.path.normpath(args["path"]))
        await ctx.slack.upload(ctx.conv.channel, ctx.conv.thread_ts, None, name,
                               args.get("comment"), data=data)
        return _text("posted")

    @tool("post_worker_file", "Fetch a file from a worker's working folder (e.g. a task's "
          "artifact) and post it into this conversation. Up to 20 MB. From a shared worker, "
          "only the files of this member's tasks: tasks/<task id>/... in its working folder.",
          {"type": "object", "properties": {"worker": {"type": "string"},
                                            "path": {"type": "string"},
                                            "comment": {"type": "string"}},
           "required": ["worker", "path"]})
    async def post_worker_file(args):
        refused = _file_refused(ctx, args["worker"], args["path"])
        if refused:
            return _err(refused)
        try:
            f = await ctx.workers.call(args["worker"], "read_file", {"path": args["path"]})
        except WorkerError as e:
            return _err(e)
        data = base64.b64decode(f["base64"])
        name = os.path.basename(f["name"]) or "file"
        try:            # a copy in the working folder, written safely (safefs)
            local = safefs.write(os.path.realpath(ctx.workdir), os.path.join(
                "from-workers", os.path.basename(args["worker"]), name), data)
        except OSError as e:
            return _err(f"couldn't keep a copy: {e}")
        await ctx.slack.upload(ctx.conv.channel, ctx.conv.thread_ts, None, f["name"],
                               args.get("comment"), data=data)
        return _text(f"posted {f['name']} (a copy is in {local})")

    @tool("send_file_to_worker", "Send a file from your working folder (e.g. a member's "
          "attachment) to a worker, before submitting a task that needs it. Returns where it "
          "is on the worker: mention that path in the task's instruction. Up to 20 MB.",
          {"worker": str, "path": str})
    async def send_file_to_worker(args):
        raw = _read_own(ctx, args["path"])
        if raw is None:
            return _err("only existing plain files in your working folder can be sent")
        full = os.path.normpath(args["path"])
        data = base64.b64encode(raw).decode()
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
