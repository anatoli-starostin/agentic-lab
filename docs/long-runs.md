# Long runs: what works, what doesn't

A long run (`start_run`, DESIGN.md §4.2) is the agent's command, approved by a human, that
outlives the agent's session. It is held to **the same rules as the agent's short
commands**; only its lifetime differs. Claude Code's sandbox applies those rules to a short
command and ends with the session, so for a run the lab applies them itself, from the same
policy (`policy/sandbox.filesystem`), and keeps it going until it ends or is stopped.

## What a run sees, the same as a short command

| | A short command and a run |
|---|---|
| Files | reads what the agent's user can read, except what's hidden below |
| Writes | only the working folder (`workdir`, `~/work` by default; a link to another disk is fine), `/tmp`, and folders granted for writing |
| Hidden | the lab's secrets (`~/.config/agentic-lab`), the service's state (`~/.local/state/agentic-lab`), `/run` (except the GPU's socket) |
| Processes | only its own: `ps`, `top`, `kill` see nothing else (bwrap's PID namespace; the unit's `PrivatePIDs=` is ignored on systemd < 257, e.g. Ubuntu 24.04's 255, where the unit still starts and logs one line, and becomes a second layer from 257 on) |
| Caches | shared, in `work/.cache` (below) |
| The agent's GitHub setup | readable: `git push` and `gh` work |

Tools installed in the home (a uv-managed Python under `~/.local/share/uv`, pyenv, a conda
install) are readable, so environments built on them start in a run as they do in a command.
A run, like a command, can't *install* into the home: do that in the working folder or ask
for a write grant.

Caches are shared with the agent's own commands, so a run reuses what a short trial already
downloaded or built (models, wheels, compiled extensions):

| Variable | Points to |
|---|---|
| `XDG_CACHE_HOME` | `work/.cache` |
| `UV_CACHE_DIR`, `PIP_CACHE_DIR` | `work/.cache/uv`, `work/.cache/pip` |
| `HF_HOME` | `work/.cache/huggingface` |
| `TORCH_EXTENSIONS_DIR`, `TRITON_CACHE_DIR`, `CUDA_CACHE_PATH` | `work/.cache/torch_extensions`, `work/.cache/triton`, `work/.cache/nv` |

These are defaults, not a fixed set: a run's command can export its own. For a tool not
listed, point its cache at `work/.cache/<tool>` (`export FOO_CACHE=$XDG_CACHE_HOME/foo`)
so its runs and commands share it the same way.

## What differs from a short command

| | Short command | Run |
|---|---|---|
| Lifetime | ends with the command and the session | until it ends or is stopped |
| Approval | inside the sandbox: none | every run, by a human |
| Network | the sandbox's allowlist (tailnet, granted domains) | not limited by the lab, except the cloud's metadata service, closed to every lab user; it resolves names itself (the resolver's file is shown even though `/run` is hidden) |
| `/tmp` | the session's | its own, emptied when it ends |
| Timed write grants into the home | applied | applied by the job's own layer; the unit's layer underneath knows only standing ones, so such a grant doesn't reach a run |
| `sudo`, setuid helpers | refused | refused (`NoNewPrivileges`) |

## What doesn't work, and what to do instead

These are the sandbox's limits, the same for a command and a run.

| What | Instead |
|---|---|
| Writing to the home outside the working folder (`pip install --user`, `git config --global`, `~/.netrc`, `wandb login`, a conda env in `~/miniconda3`) | Install into the working folder (`uv venv work/envs/x`, `conda create -p work/envs/x`); repository-local git config; pass credentials in the environment (`WANDB_API_KEY`, `HF_TOKEN`): a member gives them to the lab through the secretary, and runs get them (DESIGN.md §6.4). |
| Writing outside the working folder elsewhere (`/data`, `/mnt/...`) | A write grant for that folder. |
| Seeing or signalling other processes | `run_status`, `stop_run`. |
| Files in a run's `/tmp` after it ends | Write them to the working folder. |
| `sudo`, `crontab` | Ask an admin. |

If a run fails on one of these, its log is in `work/runs/<name>-<id>/log`.

## Stopping a run

`stop_run`, cancelling its task, or an admin's `systemctl stop` sends **SIGTERM to every
process of the run at once**, then waits up to **60 seconds** before killing whatever is
left. That minute is the job's chance to save a checkpoint and exit. The lab itself sends
each process one TERM, but your own launcher may pass it on again (`torchrun`,
`accelerate`, `mpirun`, a shell script that traps and forwards), so a worker process can
see it twice. Make the handler idempotent: note the request, and let the training loop
save and exit once. A stopped run records when its job really ended; its exit code isn't
recorded (`null`). A job still running at 60 seconds is killed; its run is recorded as
stopped with a note that it was killed.

```python
import signal, sys

stop = False
def on_term(signum, frame):
    global stop
    stop = True                     # a second TERM changes nothing

signal.signal(signal.SIGTERM, on_term)
for step in range(start, total):
    train_step()
    if stop:
        save_checkpoint(step)       # well inside the 60 s
        sys.exit(0)
```
