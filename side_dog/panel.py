from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import queue
import secrets
import shutil
import subprocess
import threading
import time
import webbrowser
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from side_dog.cli import (
    canonical_watch_roots,
    events_path,
    load_git_state,
    load_github_pr,
    load_herdr_identities,
    read_new_events,
)
from side_dog.model import SOURCE_KEY, SOURCE_LABEL, build_activity_units


PANEL_SCHEMA = "side-dog-panel-v1"
HEARTBEAT_SECONDS = 15.0
AGENT_REFRESH_SECONDS = 2.0
GITHUB_REFRESH_SECONDS = 15.0
ALLOWED_EVENT_FIELDS = {
    "agent",
    "detail",
    "effort",
    "epoch_ms",
    "git_oid",
    "github",
    "group_id",
    "kind",
    "model",
    "operation_id",
    "repeat_count",
    "session_id",
    "started_epoch_ms",
    "status",
    "timestamp",
    "title",
    "turn_id",
}


PANEL_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Side Dog</title>
<style>
:root{color-scheme:dark;--bg:#20242c;--panel:#292e38;--line:#465064;--text:#edf2f7;--muted:#9da8b9;--blue:#66b3ff;--green:#59d98e;--yellow:#f3c969;--red:#ff6b72}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}
header{position:sticky;top:0;z-index:5;padding:10px 12px;background:#20242cf2;border-bottom:1px solid var(--line);backdrop-filter:blur(8px)}
.brand{color:var(--blue);font-weight:800;letter-spacing:.08em}.status{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}.chip{padding:2px 7px;border:1px solid var(--line);border-radius:999px;color:var(--muted)}
.chip.clean,.chip.merged{color:var(--green);border-color:#397356}.chip.pending,.chip.partial{color:var(--yellow);border-color:#806b38}.chip.failed{color:var(--red);border-color:#8a4348}.chip.open{color:var(--blue);border-color:#416d91}
.controls{display:flex;gap:5px;flex-wrap:wrap;margin-top:8px}button{background:#303745;color:var(--text);border:1px solid var(--line);border-radius:5px;padding:4px 7px;font:inherit;cursor:pointer}button.active{color:var(--blue);border-color:var(--blue)}
.view-notice{margin-top:8px;padding:7px 9px;border:1px solid var(--line);border-radius:5px;background:#1d222a;font-weight:700}.view-notice[hidden]{display:none}
#roots{display:grid;grid-template-columns:1fr;gap:10px;padding:10px}.root{min-width:0;border:1px solid var(--line);border-radius:8px;background:var(--panel);overflow:hidden}.root-head{padding:8px 10px;border-bottom:1px solid var(--line)}
.root-title{font-weight:800;color:var(--blue)}.agents{display:flex;gap:5px;flex-wrap:wrap;margin-top:6px}.agent{font-size:12px;color:var(--muted);padding:2px 5px;background:#1d222a;border-radius:4px}
.timeline{padding:5px}.unit{margin:5px 0;padding:7px 8px;border-left:3px solid var(--line);background:#242a33;border-radius:4px}.unit time{color:var(--muted)}.unit a{color:inherit;text-decoration:none}.unit a:hover{text-decoration:underline}.unit.failed{border-color:var(--red)}.unit.success{border-color:var(--green)}.unit.running{border-color:var(--yellow)}
.summary{font-weight:700}.detail{color:var(--muted);overflow-wrap:anywhere}.stages{color:var(--blue);margin-top:3px}.day{margin:10px 0 5px;color:var(--blue);border-bottom:1px solid var(--line)}details>summary{cursor:pointer}.empty{padding:15px;color:var(--muted)}
body.columns #roots{grid-template-columns:repeat(var(--count),minmax(300px,1fr))}body.stack #roots{grid-template-columns:1fr}body.paused::after{content:"PAUSED";position:fixed;right:12px;bottom:12px;color:var(--yellow);background:#20242c;border:1px solid var(--yellow);padding:5px 8px;border-radius:5px}
@media(max-width:620px){header{position:static}.controls button{flex:1}body.columns #roots{grid-template-columns:1fr}}
</style></head><body class="auto"><header><div><span class="brand">SIDE DOG</span> <span id="connection">connecting…</span></div><div id="summary" class="status"></div><div class="controls">
<button data-layout="auto" class="active">auto</button><button data-layout="columns">columns</button><button data-layout="stack">stack</button><button id="expand">e expand</button><button id="filter">f all</button><button id="pause">p pause</button><button id="reverse">r oldest</button><button id="all">a all</button>
</div><div id="notice" class="view-notice" role="status" aria-live="polite" aria-atomic="true" hidden></div></header><main id="roots"></main>
<script>
const state={roots:[],units:new Map(),expanded:false,filter:'all',paused:false,newest:true,layout:'auto',focus:null,queued:[]};
const NOTICE_MS=2000;let noticeTimer=null;
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const klass=v=>{v=String(v||'').toLowerCase();return v.includes('fail')?'failed':v.includes('clean')||v.includes('success')||v.includes('merge')?'clean':v.includes('pend')||v.includes('partial')||v.includes('running')?'pending':'open'};
function when(u){const d=new Date((u.epoch||0));return Number.isNaN(+d)?'--:--':d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});}
function day(u){const d=new Date((u.epoch||0));return Number.isNaN(+d)?'Unknown':d.toLocaleDateString([],{weekday:'short',month:'short',day:'numeric'});}
function eventText(e){return [e.title,e.detail].filter(Boolean).join(' · ')}
function unitHTML(u){const e=u.events?.[u.events.length-1]||{};const status=klass(e.status);const link=u.url?`<a href="${esc(u.url)}" target="_blank" rel="noopener">`:'<span>';const close=u.url?'</a>':'</span>';
 if(u.type==='filesystem_burst'){const s=u.summary||{};const paths=(s.paths||[]).map(p=>`${esc(p[0])}${p[1]>1?' ×'+p[1]:''}`).join(' · ');return `<details class="unit ${status}" ${state.expanded?'open':''}><summary><time>${when(u)}</time> <b>Files · ${s.changes||0} changed${s.removals?' · '+s.removals+' removed':''} · ${(s.paths||[]).length} paths</b></summary><div class="detail">${paths}</div></details>`}
 if(u.type==='pipeline')return `<article class="unit ${status}"><div>${link}<time>${when(u)}</time> <span class="summary">${esc(u.title||'Delivery')}</span>${close}</div><div class="stages">${(u.stages||[]).map(esc).join(' → ')}</div></article>`;
 return `<article class="unit ${status}">${link}<time>${when(u)}</time> <span class="summary">${esc(eventText(e))}</span>${close}</article>`;}
function visibleUnits(root){let xs=[...state.units.values()].filter(u=>u.root===root.id);if(state.filter==='milestones')xs=xs.filter(u=>u.type==='pipeline'||['test','commit','push','pr','merge','issue','github','branch','worktree'].includes(u.events?.[0]?.kind));if(state.filter==='files')xs=xs.filter(u=>u.type==='filesystem_burst'||['file','config'].includes(u.events?.[0]?.kind));xs.sort((a,b)=>(a.epoch-b.epoch)||(a.id>b.id?1:-1));if(state.newest)xs.reverse();return xs;}
function rootHTML(root){const g=root.git||{},p=root.github||{};const agents=(root.agents||[]).map(a=>`<span class="agent">${esc(a.agent)} · ${esc(a.label||'unidentified')} · ${esc(a.model||'model ?')} · ${esc(a.effort||'effort ?')} · ${esc(a.status||'unknown')}</span>`).join('');let lastDay='';const units=visibleUnits(root);const rows=units.map(u=>{const d=day(u);const marker=d!==lastDay?(lastDay=d,`<div class="day">${esc(d)}</div>`):'';return marker+unitHTML(u)}).join('');const git=g.branch?`Git ${esc(g.branch)} @ ${esc(g.short_oid||'?')}`:'No Git repository';return `<section class="root" data-root="${esc(root.id)}"><div class="root-head"><div class="root-title">Watching: ${esc(root.name)}</div><div class="detail">${git}</div><div class="status">${p.number?`<span class="chip ${klass(p.state)}">PR #${p.number} ${esc(p.state)}</span>`:''}${p.ci?`<span class="chip ${klass(p.ci)}">${esc(p.ci)}</span>`:''}</div><div class="agents">${agents||'<span class="agent">no active agent</span>'}</div></div><div class="timeline">${rows||'<div class="empty">waiting for coding-agent activity…</div>'}</div></section>`;}
function columnsFit(){const count=state.focus?1:Math.max(1,state.roots.length);return innerWidth>=count*300}
function effectiveLayout(){if(state.focus)return'stack';if(state.layout==='stack')return'stack';return columnsFit()?'columns':'stack'}
function bodyClass(){return effectiveLayout()+(state.paused?' paused':'')}
function render(){document.body.className=bodyClass();document.documentElement.style.setProperty('--count',Math.max(1,state.focus?1:state.roots.length));const roots=state.focus?state.roots.filter(r=>r.id===state.focus):state.roots;document.querySelector('#roots').innerHTML=roots.map(rootHTML).join('');document.querySelector('#summary').innerHTML=`<span class="chip">Watching ${state.roots.length} root${state.roots.length===1?'':'s'}</span>`+state.roots.map(r=>`<span class="chip">${esc(r.name)}</span>`).join('');document.querySelectorAll('[data-layout]').forEach(b=>b.classList.toggle('active',b.dataset.layout===state.layout));}
function apply(message){if(state.paused){state.queued.push(message);return}if(message.type==='snapshot'){state.roots=message.roots||[];state.units=new Map((message.units||[]).map(u=>[u.id,u]));}else if(message.type==='unit'){state.units.set(message.unit.id,message.unit)}else if(message.type==='banner'){const i=state.roots.findIndex(r=>r.id===message.root.id);if(i>=0)state.roots[i]=message.root;else state.roots.push(message.root)}render();}
const es=new EventSource('events');es.addEventListener('snapshot',e=>{document.querySelector('#connection').textContent='live';apply(JSON.parse(e.data))});es.addEventListener('unit',e=>apply({type:'unit',unit:JSON.parse(e.data)}));es.addEventListener('banner',e=>apply({type:'banner',root:JSON.parse(e.data)}));es.onerror=()=>document.querySelector('#connection').textContent='reconnecting…';
function showNotice(message){const notice=document.querySelector('#notice');notice.textContent=`View changed — ${message}`;notice.hidden=false;if(noticeTimer!==null)clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>{notice.hidden=true;notice.textContent='';noticeTimer=null},NOTICE_MS)}
function layoutNotice(layout){if(layout==='auto')return'Automatic layout — roots use columns when each has at least 300 pixels; otherwise they stack.';if(layout==='columns'&&!columnsFit())return'Columns requested — the pane is too narrow, so roots are stacked.';if(layout==='columns')return'Columns view — each root has its own side-by-side timeline.';return'Stacked view — each root has its own full-width timeline.'}
function allRootsNotice(){return effectiveLayout()==='columns'?'All roots — showing one column per root.':'All roots — showing stacked root timelines.'}
function setLayout(layout){state.layout=layout;render();showNotice(layoutNotice(layout))}
function toggleExpanded(){state.expanded=!state.expanded;document.querySelector('#expand').textContent=`e ${state.expanded?'compact':'expand'}`;render();showNotice(state.expanded?'Expanded history — individual events and full delivery detail are visible.':'Compact history — related filesystem and delivery events are grouped.')}
function cycleFilter(){state.filter={all:'milestones',milestones:'files',files:'all'}[state.filter];document.querySelector('#filter').textContent=`f ${state.filter}`;render();showNotice({milestones:'Milestones only — file activity is hidden.',files:'Files only — delivery milestones are hidden.',all:'All activity — files and delivery milestones are visible.'}[state.filter])}
function togglePause(){state.paused=!state.paused;document.querySelector('#pause').textContent=`p ${state.paused?'resume':'pause'}`;if(!state.paused){const q=state.queued.splice(0);q.forEach(apply)}render();showNotice(state.paused?'Paused — collection continues; display updates are held.':'Live — held updates are now visible.')}
function toggleOrder(){state.newest=!state.newest;document.querySelector('#reverse').textContent=`r ${state.newest?'oldest':'newest'}`;render();showNotice(state.newest?'Newest first — new events appear at the top.':'Oldest first — new events appear at the bottom.')}
function showAllRoots(){state.focus=null;render();showNotice(allRootsNotice())}
function focusRoot(index){const root=state.roots[index];if(!root)return;state.focus=root.id;render();showNotice(`Focused root: ${root.name} — showing only this root.`)}
function cycleRoot(){if(!state.roots.length)return;const index=state.focus?state.roots.findIndex(r=>r.id===state.focus):-1;focusRoot((index+1)%state.roots.length)}
document.querySelectorAll('[data-layout]').forEach(b=>b.onclick=()=>setLayout(b.dataset.layout));document.querySelector('#expand').onclick=toggleExpanded;document.querySelector('#filter').onclick=cycleFilter;document.querySelector('#pause').onclick=togglePause;document.querySelector('#reverse').onclick=toggleOrder;document.querySelector('#all').onclick=showAllRoots;
window.addEventListener('keydown',e=>{if(e.key==='e')toggleExpanded();else if(e.key==='f')cycleFilter();else if(e.key==='p')togglePause();else if(e.key==='r')toggleOrder();else if(e.key==='a')showAllRoots();else if(e.key==='Tab'){e.preventDefault();cycleRoot()}else if(/^[1-9]$/.test(e.key))focusRoot(Number(e.key)-1);else return});
new ResizeObserver(()=>{document.body.className=bodyClass()}).observe(document.body);
</script></body></html>"""


def _json_fingerprint(value: Any) -> str:
    material = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode()).hexdigest()[:20]


def _root_id(root: Path) -> str:
    return hashlib.sha256(os.fspath(root).encode()).hexdigest()[:12]


def _tagged(
    records: Iterable[dict[str, Any]], root: Path, label: str
) -> list[dict[str, Any]]:
    return [
        {**record, SOURCE_KEY: os.fspath(root), SOURCE_LABEL: label}
        for record in records
    ]


def _github_web_root(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    value = completed.stdout.strip().removesuffix(".git")
    if value.startswith("git@github.com:"):
        return "https://github.com/" + value.removeprefix("git@github.com:")
    if value.startswith("https://github.com/"):
        return value
    return ""


def _event_url(event: dict[str, Any], web_root: str) -> str:
    github = event.get("github")
    if isinstance(github, dict) and isinstance(github.get("url"), str):
        return github["url"]
    explicit = event.get("url")
    if isinstance(explicit, str) and explicit.startswith(("https://", "http://")):
        return explicit
    if not web_root:
        return ""
    kind = str(event.get("kind", ""))
    detail = str(event.get("detail", ""))
    if kind == "commit":
        oid = str(event.get("git_oid") or detail.split(" · ", 1)[0])
        if oid and all(character in "0123456789abcdefABCDEF" for character in oid):
            return f"{web_root}/commit/{oid}"
    if kind in {"pr", "merge", "github"}:
        number = github.get("number") if isinstance(github, dict) else None
        if isinstance(number, int):
            return f"{web_root}/pull/{number}"
    if kind == "issue":
        digits = detail.strip().removeprefix("#")
        if digits.isdigit():
            return f"{web_root}/issues/{digits}"
    return ""


def _safe_event(event: dict[str, Any], web_root: str) -> dict[str, Any]:
    safe = {key: event[key] for key in ALLOWED_EVENT_FIELDS if key in event}
    url = _event_url(event, web_root)
    if url:
        safe["url"] = url
    return safe


def _unit_id(unit: dict[str, Any]) -> str:
    root = str(unit.get("root", ""))
    if unit.get("type") == "pipeline":
        group = unit.get("group")
        group_id = group[-1] if isinstance(group, (list, tuple)) and group else group
        material = f"{root}:pipeline:{group_id}"
    else:
        first = unit.get("events", [{}])[0]
        material = ":".join(
            str(value)
            for value in (
                root,
                unit.get("type"),
                first.get("operation_id"),
                first.get("first_epoch_ms", first.get("epoch_ms")),
                first.get("kind"),
                first.get("detail"),
            )
        )
    return hashlib.sha256(material.encode()).hexdigest()[:20]


def wire_unit(unit: dict[str, Any], web_root: str) -> dict[str, Any]:
    events = [_safe_event(event, web_root) for event in unit.get("events", [])]
    result: dict[str, Any] = {
        "id": _unit_id(unit),
        "root": str(unit.get("root", "")),
        "type": str(unit.get("type", "event")),
        "epoch": int(unit.get("epoch", 0)),
        "events": events,
    }
    for key in ("title", "stages", "summary"):
        if key in unit:
            result[key] = unit[key]
    urls = [event.get("url") for event in events if event.get("url")]
    if urls:
        result["url"] = urls[-1]
    return result


@dataclass
class PanelRoot:
    root: Path
    label: str
    path: Path
    position: int
    records: deque[dict[str, Any]]
    web_root: str
    github: dict[str, Any] | None = None
    agents: list[dict[str, str]] | None = None
    agent_refresh: Future[Any] | None = None
    github_refresh: Future[Any] | None = None
    github_branch: str | None = None
    github_refresh_branch: str | None = None
    last_agent_refresh: float = 0.0
    last_github_refresh: float = 0.0


class PanelFeed:
    def __init__(self, roots: Iterable[Path]) -> None:
        self._lock = threading.Lock()
        self.roots: list[PanelRoot] = []
        used: dict[str, int] = {}
        for root in roots:
            label = root.name
            used[label] = used.get(label, 0) + 1
            if used[label] > 1:
                label = f"{label}:{used[label]}"
            path = events_path(root)
            records, position = read_new_events(path, 0)
            self.roots.append(
                PanelRoot(
                    root=root,
                    label=label,
                    path=path,
                    position=position,
                    records=deque(records[-500:], maxlen=500),
                    web_root=_github_web_root(root),
                )
            )
        self._unit_fingerprints: dict[str, str] = {}
        self._banner_fingerprints: dict[str, str] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max(2, len(self.roots) * 2),
            thread_name_prefix="side-dog-panel",
        )

    @staticmethod
    def _agent_rows(
        identities: dict[str, dict[str, Any]], root: Path
    ) -> list[dict[str, str]]:
        unique: dict[str, dict[str, Any]] = {}
        for identity in identities.values():
            identity_root = identity.get("root")
            if not isinstance(identity_root, str):
                continue
            try:
                if Path(identity_root).expanduser().resolve() != root.resolve():
                    continue
            except OSError:
                continue
            key = identity.get("pane_id") or identity.get("label") or repr(identity)
            unique[str(key)] = identity
        return [
            {
                key: str(identity.get(key, ""))
                for key in ("agent", "label", "model", "effort", "status")
            }
            for identity in unique.values()
        ]

    def _start_external_refreshes(self, now: float, *, force: bool = False) -> None:
        for state in self.roots:
            if state.agent_refresh is None and (
                force or now - state.last_agent_refresh >= AGENT_REFRESH_SECONDS
            ):
                state.last_agent_refresh = now
                state.agent_refresh = self._executor.submit(
                    load_herdr_identities, state.root
                )
            if state.github_refresh is None and (
                force or now - state.last_github_refresh >= GITHUB_REFRESH_SECONDS
            ):
                state.last_github_refresh = now
                git = load_git_state(state.root) or {}
                state.github_refresh_branch = git.get("branch")
                state.github_refresh = self._executor.submit(load_github_pr, state.root)

    def _collect_external_refreshes(self) -> bool:
        changed = False
        for state in self.roots:
            if state.agent_refresh is not None and state.agent_refresh.done():
                try:
                    agents = self._agent_rows(state.agent_refresh.result(), state.root)
                except Exception:
                    agents = []
                state.agent_refresh = None
                if agents != state.agents:
                    state.agents = agents
                    changed = True
            if state.github_refresh is not None and state.github_refresh.done():
                try:
                    github, _ = state.github_refresh.result()
                except Exception:
                    github = None
                state.github_refresh = None
                git = load_git_state(state.root) or {}
                current_branch = git.get("branch")
                refresh_branch = state.github_refresh_branch
                state.github_refresh_branch = None
                if current_branch != refresh_branch:
                    state.last_github_refresh = 0.0
                    continue
                if github != state.github or refresh_branch != state.github_branch:
                    state.github = github
                    state.github_branch = refresh_branch
                    changed = True
        return changed

    def _wire_root(self, state: PanelRoot) -> dict[str, Any]:
        git = load_git_state(state.root) or {}
        branch = git.get("branch")
        return {
            "id": os.fspath(state.root),
            "key": _root_id(state.root),
            "label": state.label,
            "name": state.root.name,
            "path": os.fspath(state.root),
            "git": {
                key: git[key]
                for key in ("repository", "branch", "oid", "short_oid")
                if key in git
            },
            "github": state.github if state.github_branch == branch else None,
            "agents": state.agents or [],
        }

    def _units(self) -> list[dict[str, Any]]:
        units: list[dict[str, Any]] = []
        for state in self.roots:
            records = _tagged(state.records, state.root, state.label)
            units.extend(
                wire_unit(unit, state.web_root)
                for unit in build_activity_units(records, expanded_history=False)
            )
        return units

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._start_external_refreshes(time.monotonic(), force=True)
            roots = [self._wire_root(state) for state in self.roots]
            units = self._units()
            self._unit_fingerprints = {
                unit["id"]: _json_fingerprint(unit) for unit in units
            }
            self._banner_fingerprints = {
                root["id"]: _json_fingerprint(root) for root in roots
            }
            return {
                "schema": PANEL_SCHEMA,
                "type": "snapshot",
                "generated_at": datetime.now().astimezone().isoformat(),
                "roots": roots,
                "units": units,
            }

    def poll(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            changed = False
            for state in self.roots:
                records, state.position = read_new_events(state.path, state.position)
                if records:
                    state.records.extend(records)
                    changed = True
            updates: list[tuple[str, dict[str, Any]]] = []
            if changed:
                units = self._units()
                current_fingerprints = {
                    unit["id"]: _json_fingerprint(unit) for unit in units
                }
                removed = self._unit_fingerprints.keys() - current_fingerprints.keys()
                if removed:
                    self._unit_fingerprints = current_fingerprints
                    roots = [self._wire_root(state) for state in self.roots]
                    updates.append(
                        (
                            "snapshot",
                            {
                                "schema": PANEL_SCHEMA,
                                "type": "snapshot",
                                "generated_at": datetime.now().astimezone().isoformat(),
                                "roots": roots,
                                "units": units,
                            },
                        )
                    )
                for unit in units if not removed else []:
                    fingerprint = _json_fingerprint(unit)
                    if self._unit_fingerprints.get(unit["id"]) != fingerprint:
                        self._unit_fingerprints[unit["id"]] = fingerprint
                        updates.append(("unit", unit))
            now = time.monotonic()
            self._collect_external_refreshes()
            self._start_external_refreshes(now)
            for state in self.roots:
                root = self._wire_root(state)
                fingerprint = _json_fingerprint(root)
                if self._banner_fingerprints.get(root["id"]) != fingerprint:
                    self._banner_fingerprints[root["id"]] = fingerprint
                    updates.append(("banner", root))
            return updates

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def encode_sse(event: str, value: dict[str, Any]) -> bytes:
    payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode()


def localhost_host(value: str | None) -> bool:
    if not value:
        return False
    host = value.strip().lower()
    if host.startswith("["):
        hostname = host[1:].split("]", 1)[0]
    else:
        hostname = host.split(":", 1)[0]
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        token: str,
        feed: PanelFeed,
        poll_seconds: float,
    ) -> None:
        self.token = token
        self.feed = feed
        self.poll_seconds = poll_seconds
        self._state_lock = threading.Lock()
        self._subscribers: set[queue.Queue[tuple[str, dict[str, Any]]]] = set()
        self._stop_feed = threading.Event()
        super().__init__(address, PanelHandler)
        self._snapshot = self.feed.snapshot()
        self._feed_thread = threading.Thread(
            target=self._run_feed,
            name="side-dog-panel-feed",
            daemon=True,
        )
        self._feed_thread.start()

    def subscribe(
        self,
    ) -> tuple[dict[str, Any], queue.Queue[tuple[str, dict[str, Any]]]]:
        updates: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        with self._state_lock:
            self._subscribers.add(updates)
            snapshot = self._snapshot
        return snapshot, updates

    def unsubscribe(self, updates: queue.Queue[tuple[str, dict[str, Any]]]) -> None:
        with self._state_lock:
            self._subscribers.discard(updates)

    def publish(self, event: str, value: dict[str, Any]) -> None:
        with self._state_lock:
            if event == "snapshot":
                self._snapshot = value
            elif event == "unit":
                units = [
                    value if unit.get("id") == value.get("id") else unit
                    for unit in self._snapshot.get("units", [])
                ]
                if not any(unit.get("id") == value.get("id") for unit in units):
                    units.append(value)
                self._snapshot = {**self._snapshot, "units": units}
            elif event == "banner":
                roots = [
                    value if root.get("id") == value.get("id") else root
                    for root in self._snapshot.get("roots", [])
                ]
                if not any(root.get("id") == value.get("id") for root in roots):
                    roots.append(value)
                self._snapshot = {**self._snapshot, "roots": roots}
            for subscriber in self._subscribers:
                subscriber.put_nowait((event, value))

    def _run_feed(self) -> None:
        while not self._stop_feed.wait(self.poll_seconds):
            for event, value in self.feed.poll():
                self.publish(event, value)

    def server_close(self) -> None:
        self._stop_feed.set()
        self._feed_thread.join(timeout=max(1.0, self.poll_seconds * 2))
        self.feed.close()
        super().server_close()


class PanelHandler(BaseHTTPRequestHandler):
    server: PanelServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _headers(
        self, status: int, content_type: str, length: int | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _allowed(self) -> bool:
        if not localhost_host(self.headers.get("Host")):
            return False
        path = urlsplit(self.path).path
        parts = [part for part in path.split("/") if part]
        return bool(parts and secrets.compare_digest(parts[0], self.server.token))

    def do_GET(self) -> None:
        if not self._allowed():
            body = b"not found\n"
            self._headers(404, "text/plain; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        path = urlsplit(self.path).path.rstrip("/")
        base = f"/{self.server.token}"
        if path == base:
            body = PANEL_HTML.encode()
            self._headers(200, "text/html; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if path == f"{base}/events":
            self._events()
            return
        body = b"not found\n"
        self._headers(404, "text/plain; charset=utf-8", len(body))
        self.wfile.write(body)

    def _events(self) -> None:
        self.close_connection = True
        self._headers(200, "text/event-stream; charset=utf-8")
        snapshot, updates = self.server.subscribe()
        try:
            self.wfile.write(encode_sse("snapshot", snapshot))
            self.wfile.flush()
            while True:
                try:
                    event, value = updates.get(timeout=HEARTBEAT_SECONDS)
                    self.wfile.write(encode_sse(event, value))
                except queue.Empty:
                    self.wfile.write(
                        encode_sse(
                            "heartbeat",
                            {
                                "schema": PANEL_SCHEMA,
                                "epoch_ms": int(time.time() * 1000),
                            },
                        )
                    )
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            self.server.unsubscribe(updates)


def create_panel_server(
    roots: Iterable[Path], *, port: int = 0, poll_seconds: float = 0.75
) -> tuple[PanelServer, str]:
    token = secrets.token_urlsafe(24)
    server = PanelServer(
        ("127.0.0.1", port), token, PanelFeed(roots), max(0.05, poll_seconds)
    )
    url = f"http://127.0.0.1:{server.server_port}/{token}/"
    return server, url


def launch_panel(url: str) -> bool:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
    ]
    executable = next(
        (
            candidate
            for candidate in candidates
            if candidate and Path(candidate).exists()
        ),
        None,
    )
    if executable:
        try:
            subprocess.Popen(  # noqa: S603
                [executable, f"--app={url}", "--window-size=360,1040"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except OSError:
            pass
    return webbrowser.open(url)


def panel(
    projects: Iterable[str],
    *,
    port: int = 0,
    poll_seconds: float = 0.75,
    open_window: bool = True,
) -> int:
    roots = canonical_watch_roots(projects)
    server, url = create_panel_server(roots, port=port, poll_seconds=poll_seconds)
    print(f"Side Dog panel: {url}", flush=True)
    if open_window:
        launch_panel(url)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
