"""The worker's side of approvals: ask the broker, wait for the human's decision.

The broker (DESIGN.md §6.3) runs on the secretary host; the worker reaches it over the
tailnet with its own token, files a request, and waits (long polls) for the decision. The
broker enforces the human's timeout, so the worker just waits for an answer; if the broker
can't be reached, the request is denied.
"""
import asyncio

import httpx2

from .runner import Decision

POLL_WAIT_S = 25
UNREACHABLE = ("The approval service can't be reached, so actions outside the sandbox are "
               "denied for now. Find a way inside the sandbox, or report what you'd need.")


def _explain(decision):
    """What the agent is told for a decision that isn't 'allow' or 'grant'."""
    action, note = decision.get("action"), decision.get("note")
    if action == "timeout":
        return ("No one answered in time. That's a timeout, not a no: look for a way to do "
                "this inside the sandbox first, and ask again only if there's no other way.")
    msg = "A human denied this."
    if note:
        msg += f" They said: {note}"
    return msg + " Don't retry the same thing; find another way, or report it in finish_task."


class BrokerApprover:
    def __init__(self, url, token, timeout_s=600):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.timeout_s = timeout_s

    async def ask(self, task, tool_name, tool_input, violations):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        try:
            async with httpx2.AsyncClient(headers=self.headers, timeout=POLL_WAIT_S + 10) as c:
                r = await c.post(f"{self.url}/v1/requests", json={
                    "task_id": task.id, "human": task.requester, "tool": tool_name,
                    "input": tool_input, "violations": violations})
                r.raise_for_status()
                req = r.json()
                while req["state"] == "pending" and loop.time() < deadline:
                    r = await c.get(f"{self.url}/v1/requests/{req['id']}",
                                    params={"wait": POLL_WAIT_S})
                    r.raise_for_status()
                    req = r.json()
        except (httpx2.HTTPError, ValueError, KeyError):
            return Decision("deny", UNREACHABLE)
        d = req.get("decision") or {}
        if req["state"] == "pending":
            return Decision("deny", UNREACHABLE)
        if d.get("action") == "once":
            return Decision("allow")
        if d.get("action") in ("hour", "task"):
            return Decision("grant", grants=d.get("grants") or [])
        return Decision("deny", _explain(d))

    async def active_grants(self):
        try:
            async with httpx2.AsyncClient(headers=self.headers, timeout=15) as c:
                r = await c.get(f"{self.url}/v1/grants")
                r.raise_for_status()
                return r.json().get("grants") or []
        except (httpx2.HTTPError, ValueError):
            return []
