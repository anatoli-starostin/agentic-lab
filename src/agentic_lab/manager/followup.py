"""Follow the tasks a manager submitted and keep their conversations informed.

Every few seconds, for each open task: new progress lines are posted in the task's
conversation as they arrive (by code, verbatim, no LLM); when a task starts waiting for a
human's approval, the conversation is told where to look; when it finishes, the manager
agent gets a message about it and writes the wrap-up for the member.
"""
import asyncio
import logging

from .workers import WorkerError

log = logging.getLogger("agentic_lab.manager")
POLL_S = 8
TERMINAL = ("done", "failed", "cancelled")


_wrapups = set()                # wrap-ups running in the background (kept referenced)
_wrapping = set()               # task ids whose wrap-up is running (not started twice)
GIVE_UP_AFTER = 15              # rounds a worker doesn't know a task before we stop following


async def _wrapup(state, finished, conv, t, view):
    """The agent tells the member; only then is the task done (a restart before that
    re-runs the wrap-up, so the result is never lost, #58)."""
    try:
        await finished(conv, t, view)
        t.done = True
        state.save()
    except Exception:
        log.exception("wrapping up %s failed", t.task_id)
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
        if t.task_id in _wrapping:
            continue
        if not t.registered and register is not None:
            try:
                await register(t)
                t.registered = True
                state.save()
            except Exception as e:
                log.warning("registering %s with the broker: %s", t.task_id, e)
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
            log.warning("following %s: %s", t.task_id, e)
            continue
        t.misses = 0
        conv = state.conversations.get(t.conversation)
        if conv is None:
            t.done = True
            continue
        lines = view.get("progress") or []
        # new lines by the total ever added: the worker keeps only the last ones (#58)
        total = view.get("progress_total", len(lines))
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
                task = asyncio.create_task(_wrapup(state, finished, conv, t, view))
                _wrapups.add(task)
                task.add_done_callback(_wrapups.discard)
            else:
                await _wrapup(state, finished, conv, t, view)
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
