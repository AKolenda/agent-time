# Agent Time

Agent Time reads local Claude and Codex transcripts and makes import-ready work intervals available to TimeTracker. Its network address and trusted clients are set in `~/.config/agent-time.env`.

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

## Local import API

- `GET /api/v1/projects` lists Agent Time project names.
- `GET /api/v1/intervals` returns raw intervals. Optional query filters: `project`, `agent`, `start`, and `end`; timestamps accept Unix seconds or ISO-8601.
- `GET /api/v1/import` returns billable blocks grouped only within each source project. It accepts the same filters plus `gap_minutes`, which defaults to `15` (use `0` for exact transcript intervals).

The service binds only to the configured desktop LAN address and permits requests only from the configured clients, plus the desktop itself. No cloud service or API key is used.
