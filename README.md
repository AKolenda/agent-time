# Agent Time

Agent Time reads local Claude and Codex transcripts and makes import-ready work intervals available to TimeTracker. It runs at `http://127.0.0.1:8765` on the local network and accepts requests only from the TimeTracker VM at `127.0.0.1`.

Install it for the current user:

```bash
mkdir -p ~/.config/systemd/user
cp ~/Desktop/agent-time/agent-time.service ~/.config/systemd/user/agent-time.service
systemctl --user daemon-reload
systemctl --user enable --now agent-time.service
```

To have it remain available after a reboot before logging into the desktop, run once:

```bash
loginctl enable-linger "$USER"
```

Check it with `systemctl --user status agent-time.service` or open `http://127.0.0.1:8765/health`.

## Local import API

- `GET /api/v1/projects` lists Agent Time project names.
- `GET /api/v1/intervals` returns raw intervals. Optional query filters: `project`, `agent`, `start`, and `end`; timestamps accept Unix seconds or ISO-8601.
- `GET /api/v1/import` returns billable blocks grouped only within each source project. It accepts the same filters plus `gap_minutes`, which defaults to `15` (use `0` for exact transcript intervals).

The service binds only to the desktop LAN address and permits requests only from `127.0.0.1`, plus the desktop itself. No cloud service or API key is used.
