"""The worker service: the MCP API managers call, and the loop that runs tasks (DESIGN.md §5).

One process, run as the agent's own Unix user by a system service. It:
  - serves MCP (streamable HTTP) on the worker's tailnet address. Every call carries a
    manager's bearer token; the token, not anything the caller says, decides who is
    calling. A dedicated worker accepts only its own human's manager.
  - runs queued tasks one at a time, first come, first served, through an AgentRunner.
"""
import asyncio
import base64
import hmac
import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from . import tasks as T

log = logging.getLogger("agentic_lab.worker")
IDLE_POLL_S = 1.0
MAX_FILE_BYTES = 20 * 1024 * 1024       # read_file: enough for plots, reports, logs


@dataclass(frozen=True)
class Caller:
    manager: str
    human: str       # the human that manager belongs to
    readonly: bool = False      # the secretary's monitoring: status only, never changes


class AccessError(PermissionError):
    pass


class Access:
    """Who may call this worker: bearer token -> manager. `allowed_humans` is None for a
    shared worker, or the set of humans whose managers may use a dedicated one."""

    def __init__(self, tokens: dict[str, Caller], allowed_humans=None):
        self._tokens = dict(tokens)
        self.allowed_humans = allowed_humans

    def caller(self, headers) -> Caller:
        auth = (headers or {}).get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AccessError("missing bearer token")
        for known, who in self._tokens.items():
            if hmac.compare_digest(known.encode(), token.encode()):
                if (self.allowed_humans is not None and not who.readonly
                        and who.human not in self.allowed_humans):
                    raise AccessError(f"this worker is dedicated; {who.manager} may not use it")
                return who
        raise AccessError("unknown token")


def read_workdir_file(workdir, path):
    """A file from the worker's working folder (never outside it), as base64."""
    root = os.path.realpath(workdir)
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
    if full != root and not full.startswith(root + os.sep):
        raise ToolError("only files in the worker's working folder can be read")
    if not os.path.isfile(full):
        raise ToolError(f"no such file: {path}")
    size = os.path.getsize(full)
    if size > MAX_FILE_BYTES:
        raise ToolError(f"{path} is {size // 1_000_000} MB; the limit is "
                        f"{MAX_FILE_BYTES // 1_000_000} MB")
    with open(full, "rb") as f:
        data = f.read()
    return {"path": full, "name": os.path.basename(full), "size": size,
            "base64": base64.b64encode(data).decode()}


def write_inbox_file(workdir, name, b64):
    """Store a file a manager sends (e.g. a member's attachment) in the worker's
    `inbox/`, inside its working folder. Returns its path."""
    name = os.path.basename(name or "")
    if not name or name in (".", ".."):
        raise ToolError("a file name is needed")
    try:
        data = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError):
        raise ToolError("the file must be base64") from None
    if len(data) > MAX_FILE_BYTES:
        raise ToolError(f"the limit is {MAX_FILE_BYTES // 1_000_000} MB")
    inbox = os.path.join(os.path.realpath(workdir), "inbox")
    os.makedirs(inbox, exist_ok=True)
    path = os.path.join(inbox, name)
    with open(path, "wb") as f:
        f.write(data)
    return {"path": path, "size": len(data)}


def build_mcp(agent_id, store: T.TaskStore, access: Access, guardrails, health=None,
              usage=None, workdir=None, events=None):
    """The worker's MCP server. `guardrails()` returns its current settings and grants."""
    mcp = MCPServer(name=f"agentic-lab worker {agent_id}",
                    instructions="Submit tasks to this worker and follow them.")

    def who(ctx: Context, writes=False) -> Caller:
        try:
            c = access.caller(ctx.headers)
        except AccessError as e:
            raise ToolError(f"access denied: {e}") from None
        if writes and c.readonly:
            raise ToolError("access denied: this token is for monitoring only")
        return c

    @mcp.tool(description="Queue a task (first come, first served). `requester` is the "
              "human who asked for it. Returns the task.")
    def submit_task(instruction: str, requester: str, ctx: Context,
                    project: str | None = None) -> dict:
        c = who(ctx, writes=True)
        try:
            return store.submit(instruction, requester=requester, manager=c.manager,
                                project=project).view()
        except T.TaskError as e:
            raise ToolError(str(e)) from None

    def task(task_id):
        try:
            return store.get(task_id)
        except T.TaskError as e:
            raise ToolError(str(e)) from None

    @mcp.tool(description="A task's state, its latest progress line and timestamps.")
    def task_status(task_id: str, ctx: Context) -> dict:
        who(ctx)
        return task(task_id).view(with_progress=False)

    @mcp.tool(description="A task in full: all progress lines and, when finished, the result.")
    def task_result(task_id: str, ctx: Context) -> dict:
        who(ctx)
        return task(task_id).view()

    @mcp.tool(description="Cancel a task: a queued task at once, a running one as soon as "
              "the agent can stop.")
    def cancel_task(task_id: str, ctx: Context) -> dict:
        who(ctx, writes=True)
        task(task_id)
        return store.request_cancel(task_id).view(with_progress=False)

    @mcp.tool(description="All tasks on this worker, oldest first. Optionally only one state.")
    def list_tasks(ctx: Context, state: str | None = None) -> list[dict]:
        who(ctx)
        if state is not None and state not in T.STATES:
            raise ToolError(f"state must be one of {T.STATES}")
        return [t.view(with_progress=False) for t in store.all()
                if state is None or t.state == state]

    @mcp.tool(description="This worker's guardrails: its sandbox settings and grants.")
    async def get_guardrails(ctx: Context) -> dict:
        who(ctx)
        g = guardrails()
        return await g if asyncio.iscoroutine(g) else g

    @mcp.tool(description="A file from this worker's working folder (e.g. a task's artifact: "
              "a plot, a report), as base64. Up to 20 MB.")
    def read_file(path: str, ctx: Context) -> dict:
        who(ctx)
        if workdir is None:
            raise ToolError("this worker doesn't serve files")
        return read_workdir_file(workdir, path)

    @mcp.tool(description="Send this worker a file (e.g. a member's attachment), base64, up to "
              "20 MB. It lands in the worker's inbox/ folder; returns its path there.")
    def write_file(name: str, base64_data: str, ctx: Context) -> dict:
        who(ctx, writes=True)
        if workdir is None:
            raise ToolError("this worker doesn't accept files")
        return write_inbox_file(workdir, name, base64_data)

    @mcp.tool(description="This worker's own event log (messages, tool calls, results, "
              "approvals, task events) for the last `hours`, newest last. For the admin's "
              "log viewer.")
    def recent_events(ctx: Context, hours: float = 24, limit: int = 500) -> dict:
        who(ctx)
        import datetime as _dt
        if events is None:
            return {"events": []}
        return {"events": events.read(since=_dt.timedelta(hours=hours), limit=min(limit, 2000))}

    @mcp.tool(description="Liveness and state of this worker: queue, GPU, disk, versions.")
    def health_check(ctx: Context) -> dict:
        who(ctx)
        h = (health or default_health)(agent_id, store)
        if usage is not None:
            h["usage"] = usage.load()
        return h

    return mcp


def http_app(mcp: MCPServer, access: Access, allowed_hosts=None):
    """The MCP endpoint behind a bearer-token check: an unknown token gets 401 before any
    MCP handling. (Each tool checks the token again and reads the caller from it.)

    `allowed_hosts` ("name:port" entries) are the names managers use to reach this worker:
    requests with any other Host header are refused (DNS-rebinding protection)."""
    security = None
    if allowed_hosts:
        security = TransportSecuritySettings(allowed_hosts=list(allowed_hosts),
                                             allowed_origins=[])
    app = mcp.streamable_http_app(transport_security=security)

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            try:
                access.caller(headers)
            except AccessError as e:
                body = f"access denied: {e}".encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await app(scope, receive, send)

    return guarded, app


def default_health(agent_id, store):
    counts = {}
    for t in store.all():
        counts[t.state] = counts.get(t.state, 0) + 1
    gpu = None
    if shutil.which("nvidia-smi"):
        try:
            gpu = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                 "--format=csv,noheader"], capture_output=True, text=True, timeout=10
            ).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            gpu = "nvidia-smi failed"
    disk = shutil.disk_usage(os.path.expanduser("~"))
    return {"agent": agent_id, "host": platform.node(), "tasks": counts, "gpu": gpu,
            "disk_free_gb": round(disk.free / 1e9, 1)}


async def run_tasks(store: T.TaskStore, runner, stop: asyncio.Event):
    """The task loop: recover after a restart, then run queued tasks one at a time."""
    for t in store.recover():
        log.info("requeued %s after restart", t.id)
    while not stop.is_set():
        task = store.next_queued()
        if task is None:
            try:
                await asyncio.wait_for(stop.wait(), IDLE_POLL_S)
            except asyncio.TimeoutError:
                pass
            continue
        log.info("running %s for %s via %s", task.id, task.requester, task.manager)
        task = store.update(task.id, state=T.RUNNING)
        try:
            await runner.run(task, store)
        except Exception as e:      # a runner bug must not stop the worker
            log.exception("runner failed on %s", task.id)
            if store.get(task.id).state not in T.TERMINAL:
                store.update(task.id, state=T.FAILED, error=f"{type(e).__name__}: {e}")
        log.info("%s -> %s", task.id, store.get(task.id).state)
