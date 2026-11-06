"""A manager's client for its workers' MCP APIs (DESIGN.md §5)."""
import asyncio
import json

import httpx2
import mcp
from mcp.client.streamable_http import streamable_http_client


class WorkerError(RuntimeError):
    """`no_answer`: the worker's machine never answered (off, or not on the network)."""

    def __init__(self, msg, no_answer=False):
        super().__init__(msg)
        self.no_answer = no_answer


def _root_cause(e):
    """The innermost error of an (exception-group-wrapped) failure, for a readable message."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


RETRY_AFTER_S = 3       # a worker restarting (e.g. after an update) is back within seconds
CHECK_CONNECT_S = 5     # a status check: a machine that hasn't answered by then is off


class Workers:
    """`urls`: {worker-id: MCP url}; `tokens`: {worker-id: this manager's token there}."""

    def __init__(self, urls, tokens, timeout=60):
        self.urls = dict(urls)
        self.tokens = dict(tokens)
        self.timeout = timeout

    def known(self):
        return sorted(w for w in self.urls if w in self.tokens)

    async def call(self, worker, tool, args=None, retries=1, connect_timeout=None):
        """`connect_timeout`: how long to wait for the machine to answer at all (default:
        `timeout`). A machine that never answered isn't retried: it isn't restarting."""
        if worker not in self.urls or worker not in self.tokens:
            raise WorkerError(f"no worker {worker!r} (known: {', '.join(self.known())})")
        headers = {"Authorization": f"Bearer {self.tokens[worker]}"}
        timeout = httpx2.Timeout(self.timeout, connect=connect_timeout or self.timeout)
        for attempt in range(retries + 1):
            try:
                async with httpx2.AsyncClient(headers=headers, timeout=timeout) as hc:
                    async with mcp.Client(streamable_http_client(self.urls[worker],
                                                                 http_client=hc)) as c:
                        r = await c.call_tool(tool, args or {})
                break
            except Exception as e:      # unreachable, auth refused, protocol error...
                cause = _root_cause(e)
                no_answer = isinstance(cause, httpx2.ConnectTimeout)
                if attempt < retries and not no_answer:
                    await asyncio.sleep(RETRY_AFTER_S)
                    continue
                raise WorkerError(f"{worker} unreachable: {type(cause).__name__}: "
                                  f"{cause}", no_answer=no_answer) from None
        if r.is_error:
            raise WorkerError(f"{worker}: " + " ".join(b.text for b in r.content))
        items = [json.loads(b.text) for b in r.content]
        return items if tool == "list_tasks" else (items[0] if items else None)
