# Long runs: what works, what doesn't

A long run (`start_run`, DESIGN.md §4.2) is the agent's command, approved by a human, running
for hours as its own systemd unit outside the sandbox. It can do almost anything the agent's
Unix user can. This page lists the few things it can't, and how to work around each. Agents
get the short version in their instructions; this is the full list, for admins and for anyone
setting up a project.

## What a run sees

| | Inside a run |
|---|---|
| Its home (`~`, `$HOME` unchanged) | **a home of its own**, one per member (the task's requester), kept between that member's runs: writable, and empty of anything of the lab's |
| The working folder (`workdir`, `~/work` by default) | the agent's real working folder, read-write (a link to another disk is fine) |
| The agent's GitHub setup | read-only: `git push` and `gh` work as in a task |
| The lab's secrets and state | not there at all |
| `/tmp` | a private one, emptied when the run ends |
| Processes | only its own (a PID namespace): `ps`, `top`, `kill` see nothing else |
| Privileges | none gained (`NoNewPrivileges`): no `sudo`, no setuid or setgid helpers |
| Everything else | as the agent's user: other disks (`/data`, `/mnt/...`), the network, the GPUs |

Because the home is the run's own and writable, tools that keep things in `~` just work:
`pip install --user`, `git config --global` (in the run's home, on top of the lab's
setup), `~/.netrc` and `wandb login`, `huggingface-cli login`, `docker login`,
`~/.ssh/known_hosts`, npm, cargo, Go, conda and mamba (`conda create` in the home or the
working folder), IPython, Jupyter, Keras, `~/.bashrc`. What a run keeps there stays for that
member's next runs; another member's runs on the same worker don't see it.

Caches are shared with the agent's own commands, so a run reuses what a short trial in the
sandbox already downloaded or built (models, wheels, compiled extensions):

| Variable | Points to |
|---|---|
| `XDG_CACHE_HOME` | `work/.cache` |
| `UV_CACHE_DIR`, `PIP_CACHE_DIR` | `work/.cache/uv`, `work/.cache/pip` |
| `HF_HOME` | `work/.cache/huggingface` |
| `TORCH_EXTENSIONS_DIR`, `TRITON_CACHE_DIR`, `CUDA_CACHE_PATH` | `work/.cache/torch_extensions`, `work/.cache/triton`, `work/.cache/nv` |

## What doesn't work, and what to do instead

| What | Why | Instead |
|---|---|---|
| Something an admin installed into the agent's real home (e.g. `~/miniconda3` put there by hand) | the run has a home of its own | Install shared tools system-wide (`/opt`, `/usr/local`), or in the working folder. |
| Files in the agent's real home outside the working folder | same | Keep what a run needs in the working folder. |
| Files in `/tmp` meant for later | the run's `/tmp` is its own and disappears with it | Write them to the working folder (`work/tmp` is a good place). |
| Seeing or signalling other processes (another run, the agent's commands, a job started elsewhere) | its own PID namespace | Use the lab's tools: `run_status`, `stop_run`. |
| `sudo`, `crontab`, `newgrp`, other setuid or setgid helpers | no privileges can be gained | Ask an admin for anything system-wide; for recurring work, a task that starts a run. |
| The lab's secrets and state | not in the run's home, by design | A run never needs them. |

If a run fails on one of these, its log is in `work/runs/<name>-<id>/log`.

## Stopping a run

`stop_run`, cancelling its task, or an admin's `systemctl stop` sends **SIGTERM to every
process of the run at once**, then waits up to **60 seconds** before killing whatever is
left. That minute is the job's chance to save a checkpoint and exit. The lab itself sends
each process one TERM, but your own launcher may pass it on again (`torchrun`,
`accelerate`, `mpirun`, a shell script that traps and forwards), so a worker process can
see it twice. Make the handler idempotent: note the request, and let the training loop
save and exit once.

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
