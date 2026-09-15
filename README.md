# Agent Time

Agent Time reads local Claude and Codex transcripts plus T3 Code's on-device activity log. T3 Code runs are attributed to their Codex or Claude agent and retain the workspace project, so they use the same project mapping in TimeTracker. Its network address and trusted clients are set in `~/.config/agent-time.env`.

Install it for the current user:

```bash
mkdir -p ~/.config/systemd/user
cp ~/Desktop/agent-time/agent-time.env.example ~/.config/agent-time.env
cp ~/Desktop/agent-time/agent-time.service ~/.config/systemd/user/agent-time.service
systemctl --user daemon-reload
systemctl --user enable --now agent-time.service
```

To have it remain available after a reboot before logging into the desktop, run once:

```bash
loginctl enable-linger "$USER"
```

To change the desktop IP or the VM(s) allowed to import, edit `~/.config/agent-time.env` and restart the service:

```bash
systemctl --user restart agent-time.service
```

`AGENT_TIME_TRUSTED_CLIENTS` accepts a comma-separated list of IPs. Check the service with `systemctl --user status agent-time.service` or open `http://<AGENT_TIME_HOST>:8080/health`.

Run `python3 -m unittest -v` from this repository to verify source attribution and timestamp filtering.

## Client-facing chat descriptions

Agent Time writes a short, client-facing description for each chat once it has been quiet for two minutes, using the Codex CLI (`gpt-5.6-terra`, low reasoning) and T3 Code's title prompt. It runs under the desktop's own login, so no API key is needed. The description is exposed as `conversation_summary` on every interval, alongside `conversation_title`, which uses the saved T3 thread name when a native session maps to T3, and cached in `~/.cache/agent-time/summaries.json` so each chat is summarized once. Only the first few user prompts are sent to the model, never the full transcript.

Settings in `~/.config/agent-time.env`: `AGENT_TIME_SUMMARIES=0` turns this off; `AGENT_TIME_SUMMARY_MODEL`, and `AGENT_TIME_SUMMARY_EFFORT` change the model and reasoning effort. When Codex is unavailable, the tracker displays the saved T3 title.

## Local import API

- `GET /api/v1/projects` lists Agent Time project names.
- `GET /api/v1/intervals` returns raw intervals, including whether each interval came from T3 Code, Codex, or Claude plus its local conversation ID and title. Optional query filters: `project`, `agent`, `start`, and `end`; timestamps accept Unix seconds or ISO-8601.
- `GET /api/v1/import` returns billable blocks grouped only within each source project. It accepts the same filters plus `gap_minutes`, which defaults to `15` (use `0` for exact transcript intervals).

The service binds only to the configured desktop LAN address and permits requests only from the configured clients, plus the desktop itself. No cloud service or API key is used.
