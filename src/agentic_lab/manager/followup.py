"""Follow the tasks a manager submitted and keep their conversations informed.

Every few seconds, for each open task: new progress lines are posted in the task's
conversation as they arrive (by code, verbatim, no LLM); when a task starts waiting for a
human's approval, the conversation is told where to look; when its worker can't be
reached for a while (its machine stopped...), the conversation is told once, and again
when it's back; when it finishes, the manager agent gets a message about it and writes the
wrap-up for the member.
"""
import asyncio
import datetime as dt
import logging
import time

from .workers import WorkerError

log = logging.getLogger("agentic_lab.manager")
POLL_S = 8
TERMINAL = ("done", "failed", "cancelled")


_wrapups = set()                # wrap-ups running in the background (kept referenced)
_wrapping = set()               # task ids whose wrap-up is running (not started twice)
GIVE_UP_AFTER = 15              # rounds a worker doesn't know a task before we stop following
WRAPUP_TRIES = 5                # failed wrap-ups before giving up on one
WRAPUP_PAUSE_S = (30, 120, 600, 1800)   # between them
REFUSED = (400, 404, 409)       # the broker's definite "no" to a registration
REGISTER_PAUSE_MAX_S = 600      # retries back off up to this
QUIET_NOTICE_S = 120            # a worker unreachable this long is said in the conversation


async def _wrapup(state, finished, post, conv, t, view):
    """The agent writes the wrap-up, then it's posted; only then is the task done (a
    restart before that redoes what's missing, so the result is never lost, #58). The
    reply is kept once written: a failed post is retried without a new turn. A wrap-up
    that keeps failing is retried a few times, with pauses, then given up."""
    try:
        if t.wrapup_reply is None:
            t.wrapup_reply = await finished(conv, t, view) or ""
            state.save()
        if t.wrapup_reply:
            await post(conv, t.wrapup_reply)
        t.done, t.wrapup_reply = True, None
        state.save()
    except Exception:
        t.wrapup_tries += 1
        if t.wrapup_tries >= WRAPUP_TRIES:
            log.exception("wrapping up %s failed %d times: giving up", t.task_id,
                          t.wrapup_tries)
            t.done = True
        else:
            log.exception("wrapping up %s failed", t.task_id)
            t.wrapup_next = time.time() + WRAPUP_PAUSE_S[min(t.wrapup_tries,
                                                             len(WRAPUP_PAUSE_S)) - 1]
        state.save()
    finally:
        _wrapping.discard(t.task_id)


async def check_once(state, workers, post, finished, mention=lambda human: human,
                     background=False, register=None):
    """One round. `post(conv, text)` posts in a conversation; `finished(conv, task, view)`
    hands a finished task to the agent; `mention(human)` renders a human for Slack;
    `register(task)` registers a task with the broker (retried until it works).
    `background`: run each wrap-up (a whole agent turn) on its own, so a long one never
    holds up the other tasks' progress."""
    for t in state.open_tasks():
        if t.task_id in _wrapping or t.wrapup_next > time.time():
            continue
        if not t.registered and register is not None and t.register_next <= time.time():
            try:
                await register(t)
                t.registered = True
                state.save()
            except Exception as e:
                status = getattr(e, "status", None)
                if "already registered" in str(e):     # an earlier try got through
                    t.registered = True
                elif status in REFUSED:                # a definite no: retrying won't help
                    log.error("the broker refused registering %s: %s", t.task_id, e)
                    t.registered = True
                else:       # unreachable, 5xx, a token being rotated...: until it works
                    t.register_tries += 1
                    t.register_next = time.time() + min(
                        REGISTER_PAUSE_MAX_S, POLL_S * 2 ** min(t.register_tries, 10))
                    log.warning("registering %s with the broker (try %d): %s", t.task_id,
                                t.register_tries, e)
                state.save()
        try:
            view = await workers.call(t.worker, "task_result", {"task_id": t.task_id})
        except WorkerError as e:
            if "no task" in str(e):             # the worker doesn't know it (any more)
                t.misses += 1
                if t.misses >= GIVE_UP_AFTER:
                    conv = state.conversations.get(t.conversation)
                    if conv is not None:
                        await post(conv, f"⚠️ _{t.worker}_ no longer knows task {t.task_id}; "
                                         "I've stopped following it.")
                    t.done = True
                state.save()
            elif "unreachable" in str(e):
                await _unreachable(state, post, mention, t)
            log.warning("following %s: %s", t.task_id, e)
            continue
        t.misses = 0
        conv = state.conversations.get(t.conversation)
        if conv is None:
            t.done = True
            continue
        if t.unreachable_since:                 # it answers again
            if t.unreachable_noted:
                await post(conv, f"✅ _{t.worker}_ is reachable again; I'm following task "
                                 f"{t.task_id} again.")
            t.unreachable_since, t.unreachable_noted = 0.0, False
            state.save()
        lines = view.get("progress") or []
        # new lines by the total ever added: the worker keeps only the last ones (#58)
        total = max(view.get("progress_total") or 0, len(lines))
        if total < t.progress_seen:             # counted otherwise before an upgrade
            t.progress_seen = max(0, total - 1)
        new = max(0, total - t.progress_seen)
        if new > len(lines):
            await post(conv, f"🔧 _{t.worker}_ · … {new - len(lines)} earlier progress lines "
                             "not shown")
            t.progress_seen = total - len(lines)
            state.save()
        for p in lines[len(lines) - min(new, len(lines)):] if new else []:
            await post(conv, f"🔧 _{t.worker}_ · {p['message']}")
            t.progress_seen += 1                # one by one: a failed post isn't repeated
            state.save()
        st = view.get("state")
        if st == "awaiting_approval" and not t.approval_noted:
            await post(conv, f"⏳ _{t.worker}_ is waiting for an approval from "
                             f"{mention(t.requester)} in their DM from the lab's approvals app.")
            t.approval_noted = True
        elif st == "running":
            t.approval_noted = False
        t.state = st
        if st in TERMINAL:
            _wrapping.add(t.task_id)
            state.save()
            if background:
                task = asyncio.create_task(_wrapup(state, finished, post, conv, t, view))
                _wrapups.add(task)
                task.add_done_callback(_wrapups.discard)
            else:
                await _wrapup(state, finished, post, conv, t, view)
        state.save()


async def _unreachable(state, post, mention, t):
    """The worker doesn't answer: after QUIET_NOTICE_S, the conversation is told once (the
    member may need to power its machine on), instead of the manager going on silently."""
    now = time.time()
    if not t.unreachable_since:
        t.unreachable_since = now
        state.save()
    if t.unreachable_noted or now - t.unreachable_since < QUIET_NOTICE_S:
        return
    conv = state.conversations.get(t.conversation)
    if conv is None:
        return
    since = dt.datetime.fromtimestamp(t.unreachable_since, dt.timezone.utc)
    await post(conv, f"⚠️ _{t.worker}_ can't be reached since {since:%H:%M} UTC (is its "
                     f"machine stopped?). Task {t.task_id} is on hold there, not lost: "
                     f"{mention(t.requester)}, ask the secretary to power it on if it's off. "
                     "I'll say when it's back.")
    t.unreachable_noted = True
    state.save()


async def follow(state, workers, post, finished, stop: asyncio.Event, mention=None,
                 register=None):
    while not stop.is_set():
        try:
            await check_once(state, workers, post, finished,
                             mention or (lambda human: human), background=True,
                             register=register)
        except Exception:
            log.exception("follow-up round failed")
        try:
            await asyncio.wait_for(stop.wait(), POLL_S)
        except asyncio.TimeoutError:
            pass


def finished_prompt(task, view):
    r = view.get("result") or {}
    lines = [f"[lab] Task {task.task_id} on {task.worker} (for {task.requester}) has finished: "
             f"{view.get('state')}."]
    if r.get("summary"):
        lines.append(f"Worker's summary: {r['summary']}")
    if r.get("artifacts"):
        lines.append("Artifacts on the worker: " + ", ".join(r["artifacts"]))
    if view.get("error"):
        lines.append(f"Error: {view['error']}")
    lines.append("Tell the member in this conversation what came of it, briefly, and post the "
                 "files worth seeing (post_worker_file).")
    return "\n".join(lines)
