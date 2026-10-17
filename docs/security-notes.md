# Security notes

What the guardrails rely on, what is still unverified, and the trade-offs we accept. An
item stays under "To verify" until a test on a real host proves it.

## The model (DESIGN.md §6)

Every agent: inside Claude Code's built-in sandbox, Bash is auto-allowed with no internet,
the tailnet, and writes to its working folder and tmp. Outside the sandbox, internet and
writes need a human's permission; reads are free. Grants widen the sandbox for a while
(or permanently, in `lab.yaml`). There is no command parsing anywhere: a parser that
decides from command text is an arms race. Porting spiky's classifier turned up about ten
bypasses in one afternoon, and that's why we dropped the approach.

## To verify in M1

1. **GPU inside the built-in sandbox.** The docs don't mention `/dev/nvidia*`. Test:
   sandboxed `nvidia-smi` and a CUDA tensor op.
2. **Tailnet through the sandbox proxy.** The proxy refuses names that resolve only to
   local addresses. Check that `*.<tailnet>` names (100.64.0.0/10) are let through, and
   whether raw tailnet IPs work.
3. **Settings keys.** `sandbox.network.strictAllowlist` (refuse unknown hosts instead of
   prompting) and `sandbox.filesystem.allowWrite`: confirm the names and behaviour, and
   that a refused domain makes the command fail rather than prompt in a headless SDK session.
4. **Grants mid-session.** Whether a running session picks up a changed allowlist. If not,
   the service restarts and resumes the session.
5. **No silent fallback.** The Python SDK defaults `failIfUnavailable` to false. We set it
   to true; check that a broken sandbox stops the agent instead of running unsandboxed.
6. **Ubuntu 24.04+ hosts.** AppArmor blocks bubblewrap's user namespaces by default
   (`kernel.apparmor_restrict_unprivileged_userns=1`); the host setup must add the
   bwrap profile.

## Accepted trade-offs

- **Local reads are free**, including the Read tool (which runs outside the sandbox). An
  agent can read token files on its own host. Fine for small, transparent teams. A token
  that matters more moves to another Unix user, out of the agent's reach.
- **Tailnet is trusted.** Any sandboxed command can reach any tailnet host. This may
  tighten to per-host lists as the organisation grows.
- **Allowed domains can carry data out.** A domain grant (say, GitHub) also lets a
  sandboxed command upload there. The docs note domain fronting as a further limit of
  proxy-based filtering.

## Spiky's current gate (not part of this repo)

While porting, we found holes in spiky's live `cage_policy.py` and `sbox`. These include
command smuggling through the report helper, git options that run programs, write and exec
forms of "read-only" commands, and `sbox --net tailnet` keeping `CAP_NET_ADMIN`, which
lets a caged command delete its egress filter and reach the internet. Spiky needs its own
fix until it moves to the lab.
