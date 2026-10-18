#!/bin/bash
# Which people keep this machine awake: anyone with ~/.lab-keep-awake in their home.
# Run as root every minute (lab-keep-awake.timer); writes the list where the lab's services
# can read it (home folders are private). The secretary never stops a machine while the
# list isn't empty. One line per person: "<user> <first line of their file>".
out=${LAB_KEEP_AWAKE_OUT:-/var/lib/agentic-lab/keep-awake}
passwd=${LAB_PASSWD:-/etc/passwd}   # (tests point these elsewhere)
mkdir -p "$(dirname "$out")"
tmp=$(mktemp "$(dirname "$out")/.keep-awake.XXXXXX")
while IFS=: read -r user _ uid _ _ home _; do
    [ "$uid" -ge 1000 ] 2>/dev/null || continue
    f="$home/.lab-keep-awake"
    [ -f "$f" ] || continue
    printf '%s %s\n' "$user" "$(head -c 200 "$f" | head -n 1 | tr -d '\r')" >> "$tmp"
done < "$passwd"
chmod 644 "$tmp"
mv "$tmp" "$out"
