#!/usr/bin/env python3
"""Agent Time: local billing dashboard for Fable, Claude, and Codex."""
from __future__ import annotations

import argparse, csv, json, os, sys, threading, time, webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CLAUDE_ROOT = Path.home() / ".claude/projects"
CODEX_ROOT = Path.home() / ".codex/sessions"
LIVE_GRACE = 600  # An unfinished transcript is live while recently updated.
ALLOWED_CLIENTS = {"127.0.0.1", "::1"}

def configured_clients():
    return [value.strip() for value in os.environ.get("AGENT_TIME_TRUSTED_CLIENTS", "").split(",") if value.strip()]

def configured_port():
    try: return int(os.environ.get("AGENT_TIME_PORT", "8765"))
    except ValueError: return 8765

def parse_ts(value):
    try:
        if isinstance(value, (int, float)): return float(value)
        text = str(value).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError, OverflowError): return None

def real_claude_prompt(obj):
    if obj.get("type") != "user" or obj.get("isMeta"): return False
    msg = obj.get("message") or {}
    if msg.get("role") != "user": return False
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip()) and not content.lstrip().startswith("<local-command-")
    if isinstance(content, list):
        text = any(isinstance(x, dict) and x.get("type") in ("text", "input_text")
                   and str(x.get("text", "")).strip() for x in content)
        tool = any(isinstance(x, dict) and x.get("type") == "tool_result" for x in content)
        return text and not tool
    return False

@dataclass
class Interval:
    start: float
    end: float
    agent: str
    project: str
    cwd: str
    source: str
    model: str = ""
    live: bool = False

class Record:
    def __init__(self, path, kind):
        self.path, self.kind = path, kind
        self.offset = self.size = 0; self.mtime = 0.0
        self.cwd = self.model = ""; self.done = []; self.active = {}

    @property
    def project(self):
        if self.cwd: return os.path.basename(self.cwd.rstrip(os.sep)) or self.cwd
        return self.path.parent.name if self.kind == "claude" else "Unknown project"

    def reset(self):
        self.offset = self.size = 0; self.cwd = self.model = ""; self.done = []; self.active = {}

    def add(self, start, end, model=""):
        if start is None or end is None or end <= start: return
        model = model or self.model
        agent = "Codex" if self.kind == "codex" else ("Fable" if "fable" in model.lower() else "Claude")
        self.done.append(Interval(start, end, agent, self.project, self.cwd,
                                  self.kind.title(), model))

    def claude(self, obj):
        ts = parse_ts(obj.get("timestamp"))
        if obj.get("cwd"): self.cwd = str(obj["cwd"])
        if real_claude_prompt(obj) and ts is not None:
            old = self.active.pop("turn", None)
            if old and old["last"] > old["start"]: self.add(old["start"], old["last"], old["model"])
            self.active["turn"] = {"start": ts, "last": ts, "model": ""}
        elif obj.get("type") == "assistant" and ts is not None:
            model = str((obj.get("message") or {}).get("model") or "")
            if model and model != "<synthetic>": self.model = model
            if "turn" in self.active:
                self.active["turn"]["last"] = max(self.active["turn"]["last"], ts)
                if model and model != "<synthetic>": self.active["turn"]["model"] = model
        elif obj.get("type") == "system" and obj.get("subtype") == "turn_duration" and ts:
            active = self.active.pop("turn", None)
            if active:
                try: measured = ts - max(0, float(obj.get("durationMs", 0))) / 1000
                except (TypeError, ValueError): measured = active["start"]
                self.add(max(active["start"], measured), ts, active["model"])

    def codex(self, obj):
        ts, payload = parse_ts(obj.get("timestamp")), obj.get("payload") or {}
        if obj.get("type") == "session_meta":
            if payload.get("cwd"): self.cwd = str(payload["cwd"])
            return
        if obj.get("type") == "turn_context":
            if payload.get("cwd"): self.cwd = str(payload["cwd"])
            if payload.get("model"): self.model = str(payload["model"])
            return
        if obj.get("type") != "event_msg": return
        event, key = payload.get("type"), str(payload.get("turn_id") or "turn")
        if event == "task_started":
            start = parse_ts(payload.get("started_at")) or ts
            if start: self.active[key] = {"start": start, "last": ts or start, "model": self.model}
            return
        if ts:
            for active in self.active.values(): active["last"] = max(active["last"], ts)
        if event in ("task_complete", "turn_aborted", "task_cancelled"):
            active = self.active.pop(key, None)
            if not active and len(self.active) == 1: _, active = self.active.popitem()
            if active:
                self.add(parse_ts(payload.get("started_at")) or active["start"],
                         parse_ts(payload.get("completed_at")) or ts or active["last"], active["model"])

    def refresh(self):
        try: stat = self.path.stat()
        except OSError: return
        if stat.st_size < self.offset: self.reset()
        if stat.st_size == self.size and stat.st_mtime == self.mtime: return
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                while True:
                    pos, line = handle.tell(), handle.readline()
                    if not line: break
                    if not line.endswith(b"\n"): handle.seek(pos); break
                    try: obj = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError): continue
                    self.claude(obj) if self.kind == "claude" else self.codex(obj)
                self.offset = handle.tell()
        except OSError: return
        self.size, self.mtime = stat.st_size, stat.st_mtime

    def snapshot(self, now):
        result = list(self.done); recent = now - self.mtime <= LIVE_GRACE
        for active in self.active.values():
            end, live = (now, True) if recent else (active["last"], False)
            if end <= active["start"]: continue
            model = active["model"] or self.model
            agent = "Codex" if self.kind == "codex" else ("Fable" if "fable" in model.lower() else "Claude")
            result.append(Interval(active["start"], end, agent, self.project, self.cwd,
                                   self.kind.title(), model, live))
        for item in result:
            if not item.cwd and self.cwd: item.cwd, item.project = self.cwd, self.project
        return result

class Index:
    def __init__(self): self.records, self.lock = {}, threading.Lock()
    def paths(self):
        if CLAUDE_ROOT.is_dir(): yield from ((p, "claude") for p in CLAUDE_ROOT.glob("*/*.jsonl"))
        if CODEX_ROOT.is_dir(): yield from ((p, "codex") for p in CODEX_ROOT.glob("**/*.jsonl"))
    def scan(self):
        with self.lock:
            seen = set()
            for path, kind in self.paths():
                key = str(path); seen.add(key)
                if key not in self.records: self.records[key] = Record(path, kind)
                self.records[key].refresh()
            for key in set(self.records) - seen: del self.records[key]
            now = time.time(); items = []
            for record in self.records.values(): items.extend(record.snapshot(now))
            return sorted(items, key=lambda x: x.start)
    def payload(self):
        began = time.perf_counter(); items = self.scan()
        return {"now": time.time(), "timezone": datetime.now().astimezone().tzname(),
                "projects": sorted({x.project for x in items}, key=str.casefold),
                "intervals": [asdict(x) for x in items], "files": len(self.records),
                "scan_ms": round((time.perf_counter()-began)*1000)}

INDEX = Index()

API_DEFAULT_GAP_MINUTES = 15

def api_timestamp(value):
    """Accept Unix seconds or an ISO-8601 timestamp from a local API client."""
    if value is None or value == "": return None
    return parse_ts(value)

def api_intervals(params):
    """Return raw transcript intervals after optional API filters."""
    items = INDEX.scan()
    projects = {x for value in params.get("project", []) for x in value.split(",") if x}
    agents = {x.casefold() for value in params.get("agent", []) for x in value.split(",") if x}
    start = api_timestamp((params.get("start") or [None])[-1])
    end = api_timestamp((params.get("end") or [None])[-1])
    if start is not None: items = [x for x in items if x.end > start]
    if end is not None: items = [x for x in items if x.start < end]
    if projects: items = [x for x in items if x.project in projects]
    if agents: items = [x for x in items if x.agent.casefold() in agents]
    return items

def import_blocks(items, gap_minutes):
    """Join nearby intervals per source project without combining projects together."""
    gap_seconds = gap_minutes * 60
    blocks = []
    for project in sorted({item.project for item in items}, key=str.casefold):
        project_items = sorted((item for item in items if item.project == project), key=lambda item: item.start)
        current = None
        for item in project_items:
            if current and item.start <= current["end"] + gap_seconds:
                current["end"] = max(current["end"], item.end)
                current["live"] = current["live"] or item.live
                current["agents"].add(item.agent)
                current["source_intervals"].append(asdict(item))
            else:
                if current: blocks.append(current)
                current = {"start": item.start, "end": item.end, "project": project,
                           "agents": {item.agent}, "live": item.live,
                           "source_intervals": [asdict(item)]}
        if current: blocks.append(current)
    for block in blocks:
        block["seconds"] = round(block["end"] - block["start"])
        block["decimal_hours"] = round(block["seconds"] / 3600, 4)
        block["agents"] = sorted(block["agents"])
        block["start_iso"] = datetime.fromtimestamp(block["start"]).astimezone().isoformat(timespec="seconds")
        block["end_iso"] = datetime.fromtimestamp(block["end"]).astimezone().isoformat(timespec="seconds")
    return sorted(blocks, key=lambda block: block["start"])

HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Agent Time</title>
<style>
:root{--background:#09090b;--card:#0f0f12;--popover:#18181b;--border:#27272a;--input:#303034;--foreground:#fafafa;--muted:#a1a1aa;--muted-bg:#18181b;--primary:#fafafa;--primary-fg:#18181b;--green:#34d399;--fable:#a78bfa;--claude:#fb923c;--codex:#60a5fa;--radius:10px}
*{box-sizing:border-box}html{color-scheme:dark}body{margin:0;background:var(--background);color:var(--foreground);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}body:before{content:"";position:fixed;inset:0;pointer-events:none;background:radial-gradient(circle at 50% -20%,rgba(255,255,255,.055),transparent 34%)}
.shell{position:relative;max-width:1280px;margin:auto;padding:40px 28px 64px}header{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px}.brand{display:flex;gap:12px;align-items:center}.mark{width:40px;height:40px;border:1px solid #3f3f46;border-radius:var(--radius);display:grid;place-items:center;background:#fafafa;color:#18181b;box-shadow:0 1px 2px rgba(0,0,0,.35)}.mark svg{width:20px;height:20px}.brand h1{font-size:20px;line-height:1.2;font-weight:650;letter-spacing:-.025em;margin:0}.brand p,.note{font-size:12px;color:var(--muted);margin:2px 0 0}.status{display:flex;align-items:center;color:#d4d4d8;font-size:12px;background:var(--card);border:1px solid var(--border);border-radius:999px;padding:7px 11px;box-shadow:0 1px 2px rgba(0,0,0,.2)}.status.live{color:var(--foreground);border-color:#3f3f46}
.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:16px;padding:8px;background:rgba(15,15,18,.78);border:1px solid var(--border);border-radius:12px;box-shadow:0 1px 2px rgba(0,0,0,.2)}input,button{height:36px;font:inherit;color:var(--foreground);background:var(--background);border:1px solid var(--input);border-radius:8px;padding:0 11px;outline:none;transition:border-color .15s,background .15s,box-shadow .15s}input:hover,button:hover{border-color:#52525b}input:focus{border-color:#71717a;box-shadow:0 0 0 3px rgba(113,113,122,.18)}button{cursor:pointer}.dropdown{position:relative}.trigger{min-width:140px;display:flex;align-items:center;justify-content:space-between;gap:14px;text-align:left}.trigger svg{width:14px;height:14px;color:#71717a;flex:none}.projectdrop .trigger{width:190px}.menu{display:none;position:absolute;top:calc(100% + 6px);left:0;z-index:20;min-width:190px;padding:5px;background:#18181b;border:1px solid #3f3f46;border-radius:9px;box-shadow:0 14px 38px rgba(0,0,0,.5)}.menu.open{display:block;animation:menuIn .12s ease-out}@keyframes menuIn{from{opacity:0;transform:translateY(-3px)}to{opacity:1;transform:none}}.menuitem{width:100%;height:34px;display:flex;align-items:center;border:0;border-radius:6px;background:transparent;color:#e4e4e7;padding:0 9px;text-align:left}.menuitem:hover{background:#27272a;border-color:transparent}.menuitem.active:after{content:"✓";margin-left:auto;color:#a1a1aa}.projectmenu{width:310px;padding:6px}.searchbox{position:relative;margin-bottom:5px}.searchbox svg{position:absolute;left:10px;top:10px;width:15px;height:15px;color:#71717a}.searchbox input{width:100%;height:35px;padding-left:32px;border-color:transparent;background:#0f0f12}.project-options{max-height:260px;overflow:auto}.project-option{display:flex;align-items:center;border-radius:6px}.project-option:hover{background:#27272a}.project-option .menuitem{flex:1;min-width:0}.project-option .menuitem span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.project-option .remove{width:30px;height:30px;display:grid;place-items:center;padding:0;border:0;background:transparent;color:#71717a;opacity:0}.project-option:hover .remove{opacity:1}.project-option .remove:hover{color:#fafafa;background:#3f3f46}.project-option .remove svg{width:13px;height:13px}.menuempty{padding:22px 10px;text-align:center;color:var(--muted);font-size:12px}.restore{width:100%;height:32px;margin-top:5px;border:0;border-top:1px solid var(--border);border-radius:0;background:transparent;color:var(--muted);font-size:11px}.restore:hover{color:var(--foreground);border-color:var(--border)}.agents{height:36px;display:flex;align-items:center;border:1px solid var(--input);border-radius:8px;background:var(--background);padding:3px}.agents button{height:28px;border:0;padding:0 10px;color:var(--muted);background:transparent;box-shadow:none}.agents button:hover{color:var(--foreground)}.agents button.active{color:var(--foreground);background:#27272a;border-radius:6px;box-shadow:0 1px 2px rgba(0,0,0,.35)}.grow{flex:1}.rate{height:36px;display:flex;align-items:center;color:var(--muted);gap:4px;background:var(--background);border:1px solid var(--input);border-radius:8px;padding-left:11px}.rate:focus-within{border-color:#71717a;box-shadow:0 0 0 3px rgba(113,113,122,.18)}.rate input{height:32px;border:0!important;box-shadow:none!important;width:66px;padding:0 2px;background:transparent}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:12px}.card,.panel{background:linear-gradient(180deg,rgba(18,18,21,.97),rgba(13,13,16,.97));border:1px solid var(--border);border-radius:12px;box-shadow:0 1px 2px rgba(0,0,0,.24)}.card{min-height:126px;padding:20px;display:flex;flex-direction:column}.label{font-size:12px;font-weight:500;color:#d4d4d8}.value{font-size:29px;line-height:1.15;font-weight:650;letter-spacing:-.035em;margin-top:16px;font-variant-numeric:tabular-nums}.accent{color:var(--foreground)}.sub{font-size:12px;color:var(--muted);margin-top:4px}.grid{display:grid;grid-template-columns:1.65fr 1fr;gap:12px;margin-bottom:12px}.panel{padding:22px}.head{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:20px}.head h2{font-size:15px;line-height:1.3;font-weight:600;letter-spacing:-.01em;margin:0}
.chart{height:184px;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:flex-end;padding:8px 2px 0}.bw{height:100%;flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;min-width:0}.bar{width:100%;max-width:32px;min-height:2px;background:#e4e4e7;border-radius:4px 4px 1px 1px;position:relative;opacity:.9;transition:opacity .15s,transform .15s}.bar:hover{opacity:1;transform:translateY(-1px)}.bar:hover:after{content:attr(data-tip);position:absolute;bottom:calc(100% + 8px);left:50%;transform:translateX(-50%);z-index:4;background:#27272a;color:#fafafa;border:1px solid #3f3f46;padding:5px 8px;border-radius:6px;white-space:nowrap;font-size:11px;box-shadow:0 8px 24px rgba(0,0,0,.4)}.day{font-size:10px;color:var(--muted);margin-top:7px}.break{display:flex;flex-direction:column;gap:18px;padding-top:4px}.br{display:grid;grid-template-columns:66px 1fr 70px;gap:12px;align-items:center;font-size:12px}.track{height:6px;border-radius:999px;background:#27272a;overflow:hidden}.fill{height:100%;border-radius:999px}.right{text-align:right;color:#d4d4d8;font-variant-numeric:tabular-nums}
.sessions{padding:0;overflow:hidden}.sessions .head{padding:22px 22px 0}.actions{display:flex;gap:7px}.actions button{height:32px;font-size:12px;padding:0 10px;background:transparent}.actions button:first-child{background:var(--primary);border-color:var(--primary);color:var(--primary-fg);font-weight:550}.actions button:first-child:hover{background:#d4d4d8;border-color:#d4d4d8}.tablewrap{overflow:auto;max-height:640px;border-top:1px solid var(--border)}table{width:100%;border-collapse:collapse;white-space:nowrap}th{position:sticky;top:0;z-index:2;background:rgba(15,15,18,.96);backdrop-filter:blur(8px);text-align:left;text-transform:uppercase;letter-spacing:.065em;font-size:10px;font-weight:550;color:#71717a;padding:11px 20px;border-bottom:1px solid var(--border)}td{font-size:12px;padding:14px 20px;border-bottom:1px solid rgba(39,39,42,.72)}tbody tr:not(.dategroup):hover td{background:rgba(39,39,42,.23)}.dategroup td{padding:17px 20px 10px;background:#0b0b0e;border-top:1px solid var(--border);border-bottom:1px solid var(--border);color:var(--foreground);font-size:13px;font-weight:600;letter-spacing:-.01em}.dategroup:first-child td{border-top:0}.dategroup strong{float:right;color:#d4d4d8;font-size:11px;font-weight:500;letter-spacing:0;background:#18181b;border:1px solid #27272a;border-radius:999px;padding:3px 8px}.mono{font-variant-numeric:tabular-nums}.running{display:inline-flex;color:#d4d4d8;font-weight:500;background:#18181b;border:1px solid #3f3f46;border-radius:999px;padding:2px 7px}.empty{text-align:center;color:var(--muted);padding:60px!important}.project{max-width:210px;overflow:hidden;text-overflow:ellipsis}.foot{text-align:center;color:#71717a;font-size:11px;margin-top:18px}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.grow{display:none}.controls{align-items:stretch}.shell{padding:24px 16px 48px}}@media(max-width:560px){header{align-items:flex-start}.status{max-width:48%;text-align:right}.cards{grid-template-columns:1fr}.controls>select,.rate{flex:1}.agents{order:5;width:100%}.agents button{flex:1}.value{font-size:26px}.panel{padding:17px}.sessions .head{padding:18px 17px 0}.dategroup strong{float:none;display:inline-block;margin-left:8px}}
</style></head><body><div class="shell">
<header><div class="brand"><div class="mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg></div><div><h1>Agent Time</h1><p>Local billing dashboard</p></div></div><div class="status" id="status">Indexing…</div></header>
<div class="controls">
 <div class="dropdown"><button class="trigger" id="rangeTrigger" onclick="toggleMenu('rangeMenu',event)"><span id="rangeLabel">Last 30 days</span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg></button><div class="menu" id="rangeMenu"><button class="menuitem" data-value="today" onclick="setRange('today','Today')">Today</button><button class="menuitem" data-value="7" onclick="setRange('7','Last 7 days')">Last 7 days</button><button class="menuitem active" data-value="30" onclick="setRange('30','Last 30 days')">Last 30 days</button><button class="menuitem" data-value="all" onclick="setRange('all','All time')">All time</button></div></div>
 <div class="dropdown projectdrop"><button class="trigger" id="projectTrigger" onclick="toggleMenu('projectMenu',event)"><span id="projectLabel">All projects</span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg></button><div class="menu projectmenu" id="projectMenu" onclick="event.stopPropagation()"><div class="searchbox"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg><input id="projectSearch" type="search" placeholder="Search projects…" autocomplete="off" oninput="renderProjectMenu(this.value)"></div><div class="project-options" id="projectOptions"></div><button class="restore" id="restoreProjects" onclick="restoreProjects()" hidden>Restore removed projects</button></div></div>
 <div class="agents" id="agents"></div><div class="grow"></div>
 <div class="dropdown"><button class="trigger" id="gapTrigger" onclick="toggleMenu('gapMenu',event)"><span id="gapLabel">Join gaps ≤ 15 min</span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg></button><div class="menu" id="gapMenu"><button class="menuitem" data-value="0" onclick="setGap('0','Exact runs')">Exact runs</button><button class="menuitem" data-value="60" onclick="setGap('60','Join gaps ≤ 1 min')">Join gaps ≤ 1 min</button><button class="menuitem" data-value="300" onclick="setGap('300','Join gaps ≤ 5 min')">Join gaps ≤ 5 min</button><button class="menuitem active" data-value="900" onclick="setGap('900','Join gaps ≤ 15 min')">Join gaps ≤ 15 min</button></div></div>
</div>
<div class="cards"><div class="card"><div class="label">Selected time</div><div class="value accent" id="total">—</div><div class="sub" id="decimal"></div></div><div class="card"><div class="label">Continuous runs</div><div class="value" id="runs">—</div><div class="sub">X → Y activity sessions</div></div><div class="card"><div class="label">Today</div><div class="value" id="today">—</div><div class="sub">Across selected agents</div></div></div>
<div class="grid"><section class="panel"><div class="head"><div><h2>Daily coding blocks</h2><div class="note">Gaps of 15 minutes or less are included</div></div><div class="note" id="tz"></div></div><div class="chart" id="chart"></div></section><section class="panel"><div class="head"><div><h2>By agent</h2><div class="note">Coding-block time by selected agent</div></div></div><div class="break" id="break"></div></section></div>
<section class="panel sessions"><div class="head"><div><h2>Billing sessions by date</h2><div class="note">Each X→Y block includes gaps up to 15 minutes; Agent active shows raw runtime</div></div><div class="actions"><button id="csv">Export CSV</button> <button id="refresh">Refresh</button></div></div><div class="tablewrap"><table><thead><tr><th>Start → End</th><th>Block time</th><th>Agent active</th><th>Agent</th><th>Project</th><th>Status</th></tr></thead><tbody id="rows"></tbody></table></div></section><div class="foot" id="foot">Transcript text never leaves this computer.</div></div>
<script>
let D=null,agent='all',rangeValue='30',projectValue='all',gapValue=localStorage.atgapv3||'900';let hiddenProjects=new Set(JSON.parse(localStorage.athiddenprojects||'[]'));const $=x=>document.getElementById(x),C={Fable:'var(--fable)',Claude:'var(--claude)',Codex:'var(--codex)'},pad=n=>String(n).padStart(2,'0');
function midnight(t=D.now){let d=new Date(t*1000);d.setHours(0,0,0,0);return d.getTime()/1000}function dur(s,sec=false){s=Math.max(0,Math.round(s));let h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return sec?`${h}h ${pad(m)}m ${pad(x)}s`:`${h}h ${pad(m)}m`}function clock(t){return new Date(t*1000).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',hour12:true})}function date(t){return new Date(t*1000).toLocaleDateString([],{weekday:'long',month:'long',day:'numeric',year:'numeric'})}function dayId(t){let d=new Date(t*1000);return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`}function esc(s){return String(s).replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[x]))}
function start(){if(rangeValue==='all')return-Infinity;if(rangeValue==='today')return midnight();return midnight()-(+rangeValue-1)*86400}function filtered(){let s=start();return D.intervals.filter(x=>(agent==='all'||x.agent===agent)&&(projectValue==='all'||x.project===projectValue)&&x.end>s).map(x=>({...x,start:Math.max(x.start,s)}))}function union(a){let out=[];for(let x of a.map(x=>[x.start,x.end]).sort((a,b)=>a[0]-b[0])){let p=out.at(-1);p&&x[0]<=p[1]?p[1]=Math.max(p[1],x[1]):out.push(x.slice())}return out}function secs(a){return union(a).reduce((n,x)=>n+x[1]-x[0],0)}function clip(a,s,e=Infinity){return a.map(x=>({...x,start:Math.max(x.start,s),end:Math.min(x.end,e)})).filter(x=>x.end>x.start)}
function splitDays(a){let out=[];for(let x of a){let s=x.start;while(s<x.end){let next=new Date(s*1000);next.setHours(24,0,0,0);let e=Math.min(x.end,next.getTime()/1000);out.push({...x,start:s,end:e});s=e}}return out}function groups(a){let gap=+gapValue,out=[];for(let x of a.slice().sort((a,b)=>a.start-b.start)){let g=out.at(-1);if(g&&dayId(x.start)===dayId(g.start)&&x.start<=g.end+gap){g.end=Math.max(g.end,x.end);g.parts.push(x);g.live||=x.live;g.as.add(x.agent);g.ps.add(x.project)}else out.push({start:x.start,end:x.end,parts:[x],live:x.live,as:new Set([x.agent]),ps:new Set([x.project])})}return out.reverse()}
function blockSecs(a){return groups(a).reduce((n,x)=>n+x.end-x.start,0)}
function render(){let a=splitDays(filtered()),g=groups(a),total=g.reduce((n,x)=>n+x.end-x.start,0),td=blockSecs(clip(a,midnight(),D.now));$('total').textContent=dur(total,true);$('decimal').textContent=`${(total/3600).toFixed(2)} importable hours`;$('runs').textContent=g.length;$('today').textContent=dur(td,true);chart(a);breakdown(a);rows(g);localStorage.atgapv3=gapValue}
function chart(a){let n=rangeValue==='today'?1:(rangeValue==='7'?7:14),base=midnight(),ds=[];for(let i=n-1;i>=0;i--){let s=base-i*86400;ds.push({s,v:blockSecs(clip(a,s,s+86400))})}let max=Math.max(...ds.map(x=>x.v),1);$('chart').innerHTML=ds.map(x=>`<div class="bw"><div class="bar" style="height:${Math.max(2,x.v/max*100)}%" data-tip="${dur(x.v,true)}"></div><div class="day">${n===1?'Today':new Date(x.s*1000).toLocaleDateString([],{weekday:'short'})}</div></div>`).join('')}
function breakdown(a){let v=['Fable','Claude','Codex'].map(name=>({name,v:blockSecs(a.filter(x=>x.agent===name))})),max=Math.max(...v.map(x=>x.v),1);$('break').innerHTML=v.map(x=>`<div class="br"><span>${x.name}</span><div class="track"><div class="fill" style="width:${x.v/max*100}%;background:${C[x.name]}"></div></div><span class="right">${dur(x.v)}</span></div>`).join('')}
function rows(g){if(!g.length){$('rows').innerHTML='<tr><td colspan="6" class="empty">No agent activity in this selection.</td></tr>';return}let shown=g.slice(0,300),html='',last='';for(let x of shown){let label=date(x.start);if(label!==last){let same=shown.filter(y=>date(y.start)===label),subtotal=same.reduce((n,y)=>n+y.end-y.start,0);html+=`<tr class="dategroup"><td colspan="6"><span>${label}</span><strong>${dur(subtotal,true)} · ${same.length} block${same.length===1?'':'s'}</strong></td></tr>`;last=label}let as=[...x.as],ps=[...x.ps],an=as.length===1?as[0]:'Mixed',pn=ps.length===1?ps[0]:`${ps.length} projects`;html+=`<tr><td class="mono">${clock(x.start)} → ${x.live?'Now':clock(x.end)}</td><td class="mono">${dur(x.end-x.start,true)}</td><td class="mono">${dur(secs(x.parts),true)}</td><td>${an}</td><td class="project" title="${esc(pn)}">${esc(pn)}</td><td><span class="${x.live?'running':''}">${x.live?'Running':'Complete'}</span></td></tr>`}$('rows').innerHTML=html}
function closeMenus(){document.querySelectorAll('.menu.open').forEach(x=>x.classList.remove('open'))}
function toggleMenu(id,event){event.stopPropagation();let menu=$(id),open=menu.classList.contains('open');closeMenus();if(!open){menu.classList.add('open');if(id==='projectMenu'){setTimeout(()=>{$('projectSearch').focus();$('projectSearch').select()},0)}}}
function markChoice(menu,value){$(menu).querySelectorAll('.menuitem[data-value]').forEach(x=>x.classList.toggle('active',x.dataset.value===value))}
function setRange(value,label){rangeValue=value;$('rangeLabel').textContent=label;markChoice('rangeMenu',value);closeMenus();if(D)render()}
function setGap(value,label){gapValue=value;$('gapLabel').textContent=label;markChoice('gapMenu',value);closeMenus();if(D)render()}
function chooseProject(value){projectValue=value;$('projectLabel').textContent=value==='all'?'All projects':value;closeMenus();if(D){renderProjectMenu($('projectSearch').value);render()}}
function saveHidden(){localStorage.athiddenprojects=JSON.stringify([...hiddenProjects]);$('restoreProjects').hidden=!hiddenProjects.size}
function removeProject(name,event){event.stopPropagation();hiddenProjects.add(name);if(projectValue===name)chooseProject('all');saveHidden();renderProjectMenu($('projectSearch').value)}
function restoreProjects(){hiddenProjects.clear();saveHidden();renderProjectMenu($('projectSearch').value)}
function renderProjectMenu(query=''){if(!D)return;let box=$('projectOptions'),needle=query.trim().toLowerCase();box.replaceChildren();let add=(name,label,removable)=>{let row=document.createElement('div');row.className='project-option';let pick=document.createElement('button');pick.className='menuitem'+(projectValue===name?' active':'');let text=document.createElement('span');text.textContent=label;pick.append(text);pick.onclick=()=>chooseProject(name);row.append(pick);if(removable){let remove=document.createElement('button');remove.className='remove';remove.title='Remove from project list';remove.setAttribute('aria-label',`Remove ${label} from project list`);remove.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12"/></svg>';remove.onclick=e=>removeProject(name,e);row.append(remove)}box.append(row)};if(!needle)add('all','All projects',false);let projects=D.projects.filter(x=>!hiddenProjects.has(x)&&x.toLowerCase().includes(needle));projects.forEach(x=>add(x,x,true));if(!projects.length&&needle){let empty=document.createElement('div');empty.className='menuempty';empty.textContent='No projects found';box.append(empty)}$('restoreProjects').hidden=!hiddenProjects.size}
async function load(){try{let r=await fetch('/api/data',{cache:'no-store'});D=await r.json();if(projectValue!=='all'&&!D.projects.includes(projectValue)){projectValue='all';$('projectLabel').textContent='All projects'}renderProjectMenu($('projectSearch').value);$('tz').textContent=D.timezone;let live=D.intervals.filter(x=>x.live).length;$('status').classList.toggle('live',!!live);$('status').textContent=live?`${live} agent run${live===1?'':'s'} active`:`Up to date · ${D.files} files`;$('foot').textContent=`Local only · refreshed ${new Date().toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',second:'2-digit',hour12:true})} · indexed in ${D.scan_ms} ms · transcript text is never displayed or sent`;render()}catch(e){$('status').textContent='Could not refresh'}}
function exportCsv(){let out=[['Date','Start','End','Block seconds','Agent-active seconds','Agents','Projects','Status']];for(let x of groups(splitDays(filtered())))out.push([date(x.start),new Date(x.start*1000).toISOString(),x.live?'RUNNING':new Date(x.end*1000).toISOString(),Math.round(x.end-x.start),Math.round(secs(x.parts)),[...x.as].join(' + '),[...x.ps].join(' + '),x.live?'Running':'Complete']);let text=out.map(r=>r.map(v=>'"'+String(v).replaceAll('"','""')+'"').join(',')).join('\n'),a=document.createElement('a');a.href=URL.createObjectURL(new Blob([text],{type:'text/csv'}));a.download='agent-time.csv';a.click()}
$('agents').innerHTML=['all','Fable','Claude','Codex'].map((x,i)=>`<button class="${i?'':'active'}" data-a="${x}">${x==='all'?'All agents':x}</button>`).join('');$('agents').querySelectorAll('button').forEach(b=>b.onclick=()=>{agent=b.dataset.a;$('agents').querySelectorAll('button').forEach(x=>x.classList.toggle('active',x===b));render()});$('refresh').onclick=load;$('csv').onclick=exportCsv;document.addEventListener('click',closeMenus);document.addEventListener('keydown',e=>{if(e.key==='Escape')closeMenus()});let gapLabels={'0':'Exact runs','60':'Join gaps ≤ 1 min','300':'Join gaps ≤ 5 min','900':'Join gaps ≤ 15 min'};$('gapLabel').textContent=gapLabels[gapValue]||gapLabels['900'];markChoice('gapMenu',gapValue);saveHidden();load();setInterval(load,5000);
</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def permitted(self):
        return self.client_address[0] in ALLOWED_CLIENTS
    def send(self, body, kind, status=200):
        self.send_response(status); self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'")
        origin = self.headers.get("Origin", "")
        if origin.startswith(("http://localhost:", "http://127.0.0.1:")):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers(); self.wfile.write(body)
    def json(self, value, status=200):
        self.send(json.dumps(value, separators=(",", ":")).encode(), "application/json", status)
    def do_OPTIONS(self):
        if not self.permitted(): self.send_error(403); return
        self.send_response(204)
        origin = self.headers.get("Origin", "")
        if origin.startswith(("http://localhost:", "http://127.0.0.1:")):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()
    def do_GET(self):
        if not self.permitted(): self.send_error(403); return
        url = urlparse(self.path); path, params = url.path, parse_qs(url.query)
        if path == "/": self.send(HTML.encode(), "text/html; charset=utf-8")
        elif path == "/api/data": self.json(INDEX.payload())
        elif path == "/api/v1/projects":
            items = INDEX.scan()
            self.json({"projects": sorted({x.project for x in items}, key=str.casefold),
                       "generated_at": time.time(), "timezone": datetime.now().astimezone().tzname()})
        elif path == "/api/v1/intervals":
            items = api_intervals(params)
            self.json({"intervals": [asdict(item) for item in items], "count": len(items),
                       "generated_at": time.time(), "timezone": datetime.now().astimezone().tzname()})
        elif path == "/api/v1/import":
            raw_gap = (params.get("gap_minutes") or [str(API_DEFAULT_GAP_MINUTES)])[-1]
            try: gap_minutes = float(raw_gap)
            except ValueError: self.json({"error": "gap_minutes must be a number"}, 400); return
            if not 0 <= gap_minutes <= 1440:
                self.json({"error": "gap_minutes must be between 0 and 1440"}, 400); return
            items = api_intervals(params); blocks = import_blocks(items, gap_minutes)
            self.json({"intervals": blocks, "count": len(blocks), "gap_minutes": gap_minutes,
                       "generated_at": time.time(), "timezone": datetime.now().astimezone().tzname()})
        elif path == "/health": self.send(b"ok\n", "text/plain")
        else: self.send(b"Not found\n", "text/plain", 404)

def merged_seconds(items):
    pairs = sorted((x.start, x.end) for x in items); out = []
    for start, end in pairs:
        if out and start <= out[-1][1]: out[-1][1] = max(out[-1][1], end)
        else: out.append([start, end])
    return sum(b-a for a,b in out)
def fmt(value):
    h, rem = divmod(round(value), 3600); m, s = divmod(rem, 60); return f"{h}h {m:02d}m {s:02d}s"
def status():
    items = INDEX.scan(); midnight = datetime.now().astimezone().replace(hour=0,minute=0,second=0,microsecond=0).timestamp(); today=[x for x in items if x.end>midnight]
    print("Today: "+fmt(merged_seconds(today))); print("All:   "+fmt(merged_seconds(items))); print("Live:  "+str(sum(x.live for x in items)))
    for name in ("Fable","Claude","Codex"): print(f"{name:<7}"+fmt(merged_seconds([x for x in today if x.agent==name])))
def export(path):
    out=Path(path).expanduser(); out.parent.mkdir(parents=True,exist_ok=True)
    with out.open("w",newline="",encoding="utf-8") as f:
        w=csv.writer(f);w.writerow(["start","end","seconds","decimal_hours","agent","project","model","status"])
        for x in INDEX.scan(): w.writerow([datetime.fromtimestamp(x.start).astimezone().isoformat(timespec="seconds"),datetime.fromtimestamp(x.end).astimezone().isoformat(timespec="seconds"),round(x.end-x.start),f"{(x.end-x.start)/3600:.4f}",x.agent,x.project,x.model,"running" if x.live else "complete"])
    print(f"Wrote {out}")
def serve(port, browser=True, host="127.0.0.1", allowed_clients=None):
    global ALLOWED_CLIENTS
    ALLOWED_CLIENTS = {"127.0.0.1", "::1", host, *(allowed_clients or [])}
    if not CLAUDE_ROOT.is_dir() and not CODEX_ROOT.is_dir(): sys.exit("No Claude or Codex transcripts found.")
    print("Indexing local transcripts…",flush=True); INDEX.scan()
    try: server=ThreadingHTTPServer((host,port),Handler)
    except OSError as e:
        if e.errno==98:
            if browser:webbrowser.open(f"http://{host}:{port}")
            print(f"Agent Time is already running at http://{host}:{port}");return
        raise
    url=f"http://{host}:{port}";print(f"Agent Time: {url}\nLeave this window open; Ctrl+C stops the dashboard.")
    if browser:threading.Timer(.3,lambda:webbrowser.open(url)).start()
    try:server.serve_forever(.5)
    except KeyboardInterrupt:print("\nAgent Time stopped.")
    finally:server.server_close()
def main():
    p=argparse.ArgumentParser(description="GUI time tracker for Fable, Claude, and Codex");p.add_argument("command",nargs="?",choices=("gui","status","export"),default="gui");p.add_argument("output",nargs="?",default="~/agent-time.csv");p.add_argument("--port",type=int,default=configured_port());p.add_argument("--host",default=os.environ.get("AGENT_TIME_HOST", "127.0.0.1"));p.add_argument("--allow-client",action="append",default=configured_clients());p.add_argument("--no-browser",action="store_true");a=p.parse_args()
    status() if a.command=="status" else export(a.output) if a.command=="export" else serve(a.port,not a.no_browser,a.host,a.allow_client)
if __name__=="__main__":main()
