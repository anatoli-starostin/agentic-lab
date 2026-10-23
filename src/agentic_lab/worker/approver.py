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
STOP_POLL_S = 5                 # with a stop check (a task that may be cancelled)
WITHDRAWN = ("The request was withdrawn: the task was cancelled, or you stopped waiting. "
             "Don't retry it.")
UNREACHABLE = ("The approval service can't be reached, so actions outside the sandbox are "
               "denied for now. Find a way inside the sandbox, or report what you'd need.")


def _explain(decision):
    """What the agent is told for a decision that isn't 'allow' or 'grant'."""
    action, note = decision.get("action"), decision.get("note")
    if action == "timeout":
        return ("Nobody answered in time, so this wasn't run, but it wasn't refused either: the "
                "action isn't forbidden and your goal still stands. Look for a way to reach it "
                "inside the sandbox (your working folder, /tmp, allowed hosts); ask again only "
                "if there's no other way.")
    msg = "A human denied this."
    if note:
        msg += f" They said: {note}"
    return msg + " Don't retry the same thing; find another way, or report it in finish_task."


class BrokerApprover:
    def __init__(self, url, token, timeout_s=None):
        """`timeout_s`: how long the agent waits at most (None: until the human answers;
        the human's own timeout, if they set one, is enforced by the broker)."""
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.timeout_s = timeout_s

    async def ask(self, task, tool_name, tool_input, violations, stop=None, filed=None):
        """File the request and wait for its decision. `stop()` true (e.g. the task was
        cancelled), or this coroutine cancelled (e.g. a manager's turn cut off): the request
        is withdrawn, so the human isn't asked about it any more."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s if self.timeout_s else None
        req = None
        try:
            async with httpx2.AsyncClient(headers=self.headers, timeout=POLL_WAIT_S + 10) as c:
                r = await c.post(f"{self.url}/v1/requests", json={
                    "task_id": task.id, "human": task.requester, "tool": tool_name,
                    "input": tool_input, "violations": violations})
                r.raise_for_status()
                req = r.json()
                if filed is not None:           # the caller keeps the id (recovery, #58)
                    filed(req["id"])
                failures = 0
                while req["state"] == "pending":
                    if (stop is not None and stop()) or (deadline and loop.time() > deadline):
                        await self._withdraw(c, req["id"])
                        return Decision("deny", WITHDRAWN)
                    try:
                        r = await c.get(f"{self.url}/v1/requests/{req['id']}",
                                        params={"wait": STOP_POLL_S if stop else POLL_WAIT_S})
                        r.raise_for_status()
                        req, failures = r.json(), 0
                    except httpx2.HTTPError:        # the broker restarting: keep waiting
                        failures += 1
                        await asyncio.sleep(min(60, 2 ** failures))
        except asyncio.CancelledError:
            if req is not None and req.get("state") == "pending":
                try:
                    async with httpx2.AsyncClient(headers=self.headers, timeout=10) as c:
                        await self._withdraw(c, req["id"])
                except Exception:
                    pass
            raise
        except (httpx2.HTTPError, ValueError, KeyError):
            return Decision("deny", UNREACHABLE)
        d = req.get("decision") or {}
        if d.get("action") == "once":
            return Decision("allow")
        if d.get("action") in ("hour", "task"):
            return Decision("grant", grants=d.get("grants") or [])
        return Decision("deny", _explain(d))

    async def withdraw(self, rid):
        """Withdraw a request filed earlier (e.g. by this worker before it restarted).
        True once the broker answered (withdrawn, or already decided); False if it
        couldn't be reached (try again later)."""
        try:
            async with httpx2.AsyncClient(headers=self.headers, timeout=10) as c:
                r = await c.post(f"{self.url}/v1/requests/{rid}/withdraw")
            return r.status_code < 500
        except httpx2.HTTPError:
            return False

    async def _withdraw(self, client, rid):
        try:
            await client.post(f"{self.url}/v1/requests/{rid}/withdraw")
        except httpx2.HTTPError:
            pass

    async def active_grants(self):
        try:
            async with httpx2.AsyncClient(headers=self.headers, timeout=15) as c:
                r = await c.get(f"{self.url}/v1/grants")
                r.raise_for_status()
                return r.json().get("grants") or []
        except (httpx2.HTTPError, ValueError):
            return []
