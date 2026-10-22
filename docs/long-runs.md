# Long runs: what works, what doesn't

A long run (`start_run`, DESIGN.md §4.2) is the agent's command, approved by a human, running
for hours as its own systemd unit outside the sandbox. It can do almost anything the agent's
Unix user can, with a few limits that keep the lab's own things out of its reach. This page
lists what those limits break and how to work around each. Agents get the short version in
their instructions; this is the full list, for admins and for anyone setting up a project.

## The limits

| | Inside a run |
|---|---|
| The agent's home | **read-only**, except the working folder (`workdir`, `~/work` by default) |
| The working folder | read-write, as usual (a symlink to another disk is fine) |
| `~/.config/agentic-lab` | hidden (the lab's secrets) |
| `/tmp` | a private one, emptied when the run ends |
| Processes | only its own (a PID namespace): `ps`, `top`, `kill` see nothing else |
| Privileges | none gained (`NoNewPrivileges`): no `sudo`, no setuid or setgid helpers |
| Everything else | as the agent's user: other disks (`/data`, `/mnt/...`), the network, the GPUs |

So that common tools keep working, a run's environment points what they would put in the
home into the working folder:

| Variable | Points to | For |
|---|---|---|
| `XDG_CACHE_HOME` (+ `UV_CACHE_DIR`, `PIP_CACHE_DIR`, `HF_HOME`, `TORCH_EXTENSIONS_DIR`, `TRITON_CACHE_DIR`, `CUDA_CACHE_PATH`) | `work/.cache/...` | caches (#45) |
| `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME` | `work/.config`, `work/.local/share`, `work/.local/state` | tools that follow XDG (matplotlib, uv, Jupyter data, ...) |
| `PYTHONUSERBASE` | `work/.local` | `pip install --user` |
| `IPYTHONDIR`, `JUPYTER_CONFIG_DIR` | `work/.config/ipython`, `work/.config/jupyter` | IPython, Jupyter |
| `KERAS_HOME` | `work/.cache/keras` | Keras datasets and models |
| `npm_config_cache`, `npm_config_prefix` | `work/.cache/npm`, `work/.local` | npm, `npm install -g` |
| `CARGO_HOME`, `GOPATH` | `work/.cache/cargo`, `work/.cache/go` | Rust and Go downloads and builds |
| `CONDA_PKGS_DIRS`, `CONDA_ENVS_PATH` | `work/.cache/conda/pkgs`, `work/envs` | conda and mamba packages and new environments |

## What doesn't work, and what to do instead

| What | Why | Instead |
|---|---|---|
| Installing into an environment that lives in the home (`~/miniconda3`, `~/.venv`, a `pyenv` version) | the home is read-only | Use it as it is (activating and running work), or create the environment in the working folder: `uv venv work/.venv`, `conda create -p work/envs/x`. Better: prepare environments in a short task first, then start the run. |
| `pip install --user` into `~/.local` | read-only | It goes to `work/.local` instead (`PYTHONUSERBASE`); a venv in the working folder is cleaner. |
| `git config --global` | the global git config is the lab's (`GIT_CONFIG_GLOBAL`, read-only) | Use the repository's own config: `git config user.name ...` inside the repo, or `git -c key=value ...`. Pushing with the agent's GitHub identity works as usual. |
| `~/.netrc`, `wandb login`, `huggingface-cli login`, `docker login`, other "log in once" tools | they write credentials into the home | Pass the credential in the environment for that run (`WANDB_API_KEY`, `HF_TOKEN`), or point the tool at a file in the working folder (`NETRC=work/.netrc` for tools that honour it). Keep such files out of shared folders. |
| `ssh` to a new host | it can't add to `~/.ssh/known_hosts` | `ssh -o UserKnownHostsFile=work/.ssh/known_hosts ...`, or HTTPS for git. |
| `~/.bashrc`, `~/.profile`, shell aliases | read-only, and a run doesn't read them | Put what the job needs in its own script. |
| Tools that insist on a fixed path in the home (e.g. `~/.something` with no variable to move it) | read-only | Usually an option or variable moves it; otherwise run the tool with `HOME=work/home` for that command only (the agent's GitHub setup still works: it's found through its own variables). |
| `nltk.download()`, `torch.hub` and similar downloaders | `nltk` defaults to `~/nltk_data` | `NLTK_DATA=work/.cache/nltk`; `torch.hub` already follows `XDG_CACHE_HOME`. |
| Files in `/tmp` meant for later | the run's `/tmp` is its own and disappears with it | Write them to the working folder (`work/tmp` is a good place). |
| Seeing or signalling other processes (another run, the agent's commands, a training job started elsewhere) | its own PID namespace | Use the lab's tools: `run_status`, `stop_run`. |
| `sudo`, `crontab`, `systemctl --user`, `newgrp` | no privileges can be gained | Ask an admin for anything system-wide; for recurring work, a task that starts a run. |
| The lab's secrets (`~/.config/agentic-lab`) | hidden by design | A run never needs them. |

If a run fails because of one of these, its log in `work/runs/<name>-<id>/log` normally says
"Read-only file system" or "Permission denied" for a path in the home.
