# Agentic Lab: the member client

`lab-member.py` is the one file a human member of a lab ever needs to run. It is plain
Python (3.8+, standard library only), so there is nothing to install.

The lab's secretary sends you a message like:

> Please run on your computer (you need to be on the lab's tailnet):
> `curl -fsSL http://secretary.example.ts.net:8771/client/lab-member.py -o lab-member.py && python3 lab-member.py claude-token --code K7QM-XP4D --to http://secretary.example.ts.net:8771`

What it does, for `claude-token`:
1. runs `claude setup-token` and picks the token up from its output (it shows you a
   masked version to confirm). If the token was shown elsewhere, e.g. in your browser,
   you paste it at a hidden prompt; `--paste` skips `setup-token` for a token you
   already have;
2. the token never goes into Slack or any chat;
3. sends it to the secretary over the tailnet, with the one-time code. The code works
   once, for this one secret, for a limited time (24 hours by default).

Your agents then run on your Claude seat. The token is valid for a year; to renew it, ask
the secretary (in Slack) for a new link and run the same command again. (An admin can list
its expiry date under `credentials` in `lab.yaml` so the secretary reminds everyone.)
