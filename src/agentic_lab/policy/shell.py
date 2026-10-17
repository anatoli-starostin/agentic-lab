"""Shell-line parsing for the policy engine.

Security-critical and policy-free: nothing here knows what is allowed. It only answers
structural questions about a shell line: what are its top-level segments, and does it
carry an operator that escapes a wrapped command (a redirect to a real path, a command
substitution, a background `&`)?

Ported verbatim (logic) from spiky's agent-cage `cage_policy.py`.
"""
import shlex

# punctuation_chars=True makes shlex ISOLATE the operators ();<>|& into their own tokens
# even when attached to a word (foo|curl -> foo | curl), while keeping operators INSIDE
# quotes literal ('a|b' stays one token). That reliability is what lets us trust segment
# boundaries -- plain shlex.split merges foo|curl into one token.
_SEPS = {"|", "||", "&&", ";", "&"}


def segments(command):
    """Split a shell line into top-level command segments (token lists).
    Returns None if unparseable."""
    cmd = command.strip()
    if not cmd:
        return []
    try:
        lx = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lx.whitespace_split = True
        tokens = list(lx)
    except ValueError:
        return None
    if not tokens:
        return []
    parts, cur = [], []
    for t in tokens:
        if t in _SEPS:
            if cur:
                parts.append(cur)
                cur = []
        else:
            cur.append(t)
    if cur:
        parts.append(cur)
    return parts


def base_tokens(command):
    """First token of each top-level segment: 'touch x && chmod y' -> ['touch', 'chmod']."""
    return [seg[0] for seg in (segments(command) or []) if seg]


def _read_redirect(s, i):
    """At s[i] = start of a redirect operator run (< > &), parse operator + target.
    Returns (next_index, safe): safe iff the redirect writes NOTHING real -- the target
    is /dev/null, or it's a file-descriptor dup (2>&1, >&2)."""
    n = len(s)
    j = i
    while j < n and s[j] in "<>&":
        j += 1
    op = s[i:j]
    k = j
    while k < n and s[k] in " \t":
        k += 1
    if k < n and s[k] in "'\"":
        q = s[k]
        k += 1
        start = k
        while k < n and s[k] != q:
            if q == '"' and s[k] == "\\" and k + 1 < n:
                k += 2
                continue
            k += 1
        target = s[start:k]
        if k < n:
            k += 1
    else:
        start = k
        while k < n and s[k] not in " \t\n<>|&;'\"`":
            k += 1
        target = s[start:k]
    safe = target == "/dev/null" or ("&" in op and target.isdigit())
    return k, safe


def escape_ops(s):
    """True if the line has, at the OUTER shell level, a redirect to a real path, a
    command substitution ($(...) or `...`), or a background '&' -- the operators that let
    a wrapped command write outside the cage or spawn an uncaged process.

    Honors quoting exactly as bash does:
      single quotes -> everything literal;
      double quotes -> $(...) and `...` still active, but < > & literal;
      unquoted      -> redirects, `, $( and a lone & are active.
    Harmless outer redirects (/dev/null, fd-dups like 2>&1) are allowed. Pipes, ;, &&
    and || are NOT flagged here (segmentation handles those). Unbalanced quotes -> True.
    """
    i, n, state = 0, len(s), None  # state: None | "'" | '"'
    while i < n:
        c = s[i]
        if state == "'":
            if c == "'":
                state = None
            i += 1
            continue
        if state == '"':
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                state = None
                i += 1
                continue
            if c == "`":
                return True
            if c == "$" and i + 1 < n and s[i + 1] == "(":
                return True
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "'":
            state = "'"
            i += 1
            continue
        if c == '"':
            state = '"'
            i += 1
            continue
        if c == "`":
            return True
        if c == "$" and i + 1 < n and s[i + 1] == "(":
            return True
        if c in "<>":
            k, safe = _read_redirect(s, i)
            if not safe:
                return True
            i = k
            continue
        if c == "&":
            if i + 1 < n and s[i + 1] == "&":   # && chain -> segmentation handles it
                i += 2
                continue
            if i + 1 < n and s[i + 1] == ">":   # &> redirect (both streams)
                k, safe = _read_redirect(s, i)
                if not safe:
                    return True
                i = k
                continue
            return True                          # lone & -> background
        i += 1
    return state is not None
