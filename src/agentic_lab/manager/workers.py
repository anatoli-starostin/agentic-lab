"""A manager's client for its workers' MCP APIs (DESIGN.md §5)."""
import json

import httpx2
import mcp
from mcp.client.streamable_http import streamable_http_client


class WorkerError(RuntimeError):
    pass


class Workers:
    """`urls`: {worker-id: MCP url}; `tokens`: {worker-id: this manager's token there}."""

    def __init__(self, urls, tokens, timeout=60):
        self.urls = dict(urls)
        self.tokens = dict(tokens)
        self.timeout = timeout

    def known(self):
        return sorted(w for w in self.urls if w in self.tokens)

    async def call(self, worker, tool, args=None):
        if worker not in self.urls or worker not in self.tokens:
            raise WorkerError(f"no worker {worker!r} (known: {', '.join(self.known())})")
        headers = {"Authorization": f"Bearer {self.tokens[worker]}"}
        try:
            async with httpx2.AsyncClient(headers=headers, timeout=self.timeout) as hc:
                async with mcp.Client(streamable_http_client(self.urls[worker],
                                                             http_client=hc)) as c:
                    r = await c.call_tool(tool, args or {})
        except Exception as e:          # unreachable, auth refused, protocol error...
            raise WorkerError(f"{worker} unreachable: {type(e).__name__}: {e}") from None
        if r.is_error:
            raise WorkerError(f"{worker}: " + " ".join(b.text for b in r.content))
        items = [json.loads(b.text) for b in r.content]
        return items if tool == "list_tasks" else (items[0] if items else None)
