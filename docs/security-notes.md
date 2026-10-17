# Security notes

Known gaps in the guardrails and how we plan to close them. A gap stays listed here until
a test proves it's closed.

## Open

### 1. Green `git` runs repository-controlled code outside the cage

Workers may run everyday `git` (status, commit, push, …) without approval, **outside**
the cage, because `push` and `pull` need the network. But git reads the repository's own
`.git/config` and `.git/hooks/`, and a worker can write both from inside the cage (its
repos live in the writable zone). So a caged step can plant, for example, a
`pre-commit` hook, `core.fsmonitor`, `diff.external` or `core.sshCommand`. The next green
`git commit` or `git diff` then runs that program uncaged, with no approval.

The classifier can't see this: the command line is innocent, and the danger sits in
files. Planned fix (M1), one of:
- the gate **rewrites** green git calls (a PreToolUse hook may return an updated input)
  to `git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c diff.external= …`, so
  repository config can't name a program to run; or
- green git goes through a trusted `lab git` helper that does the same, and plain `git`
  becomes gated.

Either way, the test is: a hook planted from inside the cage never runs on a green call.

### 2. The cage's writable dirs are hard-coded in `sbox`

The policy's `writable` list and `sbox`'s writable binds must match, but `sbox` hard-codes
`~/projects`, `/tmp` and `~/.cache`. A role or overlay that changes `writable` (for
example, the manager's `~/scratch`) makes the two disagree. Planned fix (M1): `sbox` reads
its binds from the agent's effective policy (`lab policy show`), so there is one source.

### 3. Read-only tools can read secrets

`Read`, `Grep` and `cat` are ungated, so an agent can read any file its Unix user can,
including token files. The design answer (DESIGN.md §6.4) is the Unix user split: each
agent runs as its own user, and secrets belong to the service users that need them
(chmod 600). Until agents run as separate users (M1+), this holds only by convention.

## Closed while porting spiky's `cage_policy` (2026-10-17)

Each has a regression test in `tests/test_policy_engine.py`.

- **Report helper smuggling.** The whole-line rule for the report helper accepted any
  non-space id and any non-space prefix before `python3`. A shell command could ride
  along on the id (`done x;<command>`), in front of the interpreter, or after an early
  heredoc delimiter. Ids are now plain words, the interpreter path is restricted, and the
  delimiter must close the heredoc exactly once.
- **Untrusted interpreters.** `<writable>/python3 <trusted helper>` ran the agent's own
  "python3" uncaged. A helper's interpreter must now be `python3` from `PATH` or an
  absolute path outside the writable zone.
- **Program-running git options after the subcommand.** `git fetch --upload-pack=…`,
  `git push --receive-pack=…` and `git rebase -x/--exec …` were green. The engine now
  checks these options wherever they appear. `git … --output=<file>` (writes anywhere)
  is gated too.
- **Read-only commands with write or exec forms.** `awk` (via `system()`), `find -exec` and
  `-delete`, `sed`'s `w` and `e` commands, `sort -o`, a second argument to `uniq`,
  `rg --pre`, `date -s`, `ip … set`, `journalctl --vacuum`, `nvidia-smi -pl`, `env <cmd>`,
  `chmod +x` and `mkdir -p` were green anywhere. They are now gated, or pinned to their
  read-only forms. Use the cage (`sbox …`) for the rest.
