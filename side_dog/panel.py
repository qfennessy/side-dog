from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from side_dog.board import (
    BoardMessage,
    board_rows_payload,
    browser_conflicts,
    rows_from_sources,
    sort_rows as sort_board_rows,
)
from side_dog.cli import (
    BOARD_DISCOVERY_SECONDS,
    DEFAULT_GITHUB_POLL_SECONDS,
    BoardGithubRequest,
    BoardRootState,
    DiscoveryMode,
    agent_working_folders,
    board_source,
    build_worktree_inventories,
    busy_worktrees,
    collect_board_github,
    create_poll_coordinator,
    discovered_watch_roots,
    events_path,
    folder_is_finished,
    folder_discovery_mode,
    discovery_mode_from_key,
    github_refresh_due,
    herdr_session_roots,
    initial_watch_roots,
    inventory_branch_names,
    is_definitive_no_pr,
    keep_one_root,
    load_agent_identities,
    load_config,
    load_display_settings,
    load_git_state,
    load_github_pr,
    load_startup_history,
    pinned_folders,
    read_new_events,
    reconcile_herdr_roots,
    rediscovered_roots,
    refresh_board_root,
    refreshed_usage_contexts,
    save_filesystem_activity_setting,
    usage_session_keys,
    watch_root_limit,
)
from side_dog.config import (
    BOARD_DEFAULTS,
    config_board,
    config_display,
    config_notify_enabled,
)
from side_dog.integrations import AgentIdentity
from side_dog.model import (
    SOURCE_KEY,
    SOURCE_LABEL,
    agent_session_key,
    build_activity_units,
    display_model,
)
from side_dog.notify import notify_for_event
from side_dog.privacy import SAFE_PANEL_WIRE_FIELDS
from side_dog.polling import PollCoordinator, PollTarget
from side_dog.usage import UsageMonitor, usage_summary_wire


PANEL_SCHEMA = "side-dog-panel-v1"
HEARTBEAT_SECONDS = 15.0
AGENT_REFRESH_SECONDS = 2.0
GITHUB_REFRESH_SECONDS = 60.0
# Persistence already enforces this policy. The panel derives its defense-in-
# depth allowlist from the same type, adding only aggregation metadata created
# after history is read.
ALLOWED_EVENT_FIELDS = SAFE_PANEL_WIRE_FIELDS | {"first_timestamp", "repeat_count"}


def configured_filesystem_activity() -> bool:
    """Resolve the panel's display default with remembered settings winning."""
    saved = load_display_settings()
    if "show_filesystem_activity" in saved:
        return saved.get("show_filesystem_activity") is True
    configured = config_display(load_config()).get("show_filesystem_activity")
    return configured is True


PANEL_HIGHWAY_LOGIC_JS = r"""
const HIGHWAY_LANES=['files','tests','git','GitHub'];
const HIGHWAY_BASE_PX_PER_MS=0.004,HIGHWAY_HEIGHT_PX=240;
function highwayLane(event){const kind=String(event?.kind||'').toLowerCase();if(['file','config'].includes(kind))return'files';if(kind==='test')return'tests';if(['branch','worktree','commit','push'].includes(kind))return'git';if(['pr','merge','issue','github'].includes(kind))return'GitHub';return null}
function highwayJudgment(status){status=String(status||'unknown').toLowerCase();if(status==='success')return'PASS';if(status==='failed')return'MISS';if(status==='running')return'LIVE';return'NEUTRAL'}
function highwayStatus(event){const status=String(event?.status||'unknown').toLowerCase();return['success','failed','running'].includes(status)?status:'unknown'}
function isPassiveFilesystemEvent(event){const kind=String(event?.kind||'').toLowerCase();return String(event?.agent||'').toLowerCase()==='filesystem'&&['file','config'].includes(kind)}
function isLifecycleEvent(event){return String(event?.kind||'').toLowerCase()==='lifecycle'}
function backgroundEventAllowed(event,showFilesystemActivity=false){return showFilesystemActivity||(!isPassiveFilesystemEvent(event)&&!isLifecycleEvent(event))}
function highwayEventAllowed(event,filter,showFilesystemActivity=false){if(!backgroundEventAllowed(event,showFilesystemActivity))return false;const kind=String(event?.kind||'').toLowerCase();const file=['file','config'].includes(kind);if(filter==='files')return file;if(filter==='milestones')return!file;return true}
function latestHighwayCandidates(units,rootId,filter,showFilesystemActivity=false){const selected=new Map();for(const unit of units){if(unit.root!==rootId)continue;for(const [index,event] of (unit.events||[]).entries()){if(!highwayEventAllowed(event,filter,showFilesystemActivity))continue;const lane=highwayLane(event);if(!lane)continue;const epoch=Number(event.epoch_ms||unit.epoch||0);const key=event.operation_id?`operation:${event.operation_id}`:`unit:${unit.id}:${index}:${epoch}:${event.kind||''}`;const prior=selected.get(key);if(!prior||epoch>=prior.epoch)selected.set(key,{unit,event,lane,epoch,key})}}return[...selected.values()]}
function highwaySnapshot(units,rootId,nowMs,speed,filter='all',showFilesystemActivity=false){const candidates=latestHighwayCandidates(units,rootId,filter,showFilesystemActivity);const pixelsPerMs=HIGHWAY_BASE_PX_PER_MS*speed;const marks=candidates.map(({unit,event,lane,epoch,key})=>{const status=highwayStatus(event);const running=status==='running';const started=Number(event.started_epoch_ms||epoch);const age=Math.max(0,nowMs-epoch);return{id:`${unit.id}:${key}`,root:rootId,lane,status,judgment:highwayJudgment(status),epoch,y:running?0:Math.round(age*pixelsPerMs),hold:running?Math.max(4,Math.round(Math.max(0,nowMs-started)*pixelsPerMs)):0,detail:[event.title,event.detail].filter(Boolean).join(' · '),url:event.url||''}}).filter(mark=>mark.status==='running'||mark.y<=HIGHWAY_HEIGHT_PX);const stacks=new Map();for(const mark of marks){const bucket=`${mark.lane}:${Math.round(mark.y/8)}`;const index=stacks.get(bucket)||0;stacks.set(bucket,index+1);mark.offset=((index%5)-2)*10;mark.showJudgment=mark.status!=='success'||mark.y<24}let combo=0;for(const {event} of candidates.sort((a,b)=>a.epoch-b.epoch)){const status=highwayStatus(event);if(status==='success')combo+=1;else if(status==='failed')combo=0}return{marks,combo}}
function highwayShouldAnimate(view,paused,reducedMotion){return view==='highway'&&!paused&&!reducedMotion}
function highwayFreezeTimestamp(paused,reducedMotion,current,nowMs){return paused||reducedMotion?(current??nowMs):null}
function timelineOrderNotice(newest){return newest?'Timeline — activity is shown as newest-first detail rows.':'Timeline — activity is shown as oldest-first detail rows.'}
function clockTime(value){const d=new Date(value||0);return Number.isNaN(+d)?'--:--':d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}
function when(u){return clockTime(u.epoch)}
function eventWhen(u,e){const latest=when(u);const repeats=Number(e?.repeat_count||1);if(repeats<=1||!e?.first_timestamp)return latest;const first=clockTime(e.first_timestamp);return first==='--:--'?latest:`${first}→${latest}`}
function lineChanges(e){return Number.isInteger(e.lines_added)&&Number.isInteger(e.lines_removed)?`+${e.lines_added}/-${e.lines_removed}`:''}
function eventText(e){const text=[e.title,e.detail,lineChanges(e)].filter(Boolean).join(' · ');const repeats=Number(e.repeat_count||1);return repeats>1?`${text} · ×${Math.floor(repeats)}`:text}
function semanticStatus(value){const status=String(value||'unknown').toLowerCase();if(['success','completed','done','finished','clean','merged'].includes(status))return{role:'success',glyph:'✓',label:'completed'};if(status==='working')return{role:'running',glyph:'…',label:'working'};if(['running','pending'].includes(status))return{role:'running',glyph:'…',label:'running'};if(['warning','partial'].includes(status))return{role:'warning',glyph:'!',label:'warning'};if(['failed','blocked','error'].includes(status))return{role:'failed',glyph:'×',label:status==='blocked'?'blocked':'failed'};if(status==='idle')return{role:'idle',glyph:'○',label:'idle'};return{role:'unknown',glyph:'·',label:'unknown'}}
function statusMarker(value){const state=semanticStatus(value);return`<span class="state-mark ${state.role}" title="${state.label}" aria-label="${state.label}">${state.glyph}</span>`}
function agentStatusHTML(value){const state=semanticStatus(value);return`<span class="agent-state ${state.role}">${state.glyph} ${state.label}</span>`}
function agentIsIdle(agent){return String(agent&&agent.status||'').toLowerCase()==='idle'}
function visibleAgents(agents,showIdle){const list=agents||[];return showIdle?list:list.filter(a=>!agentIsIdle(a))}
function hiddenIdleCount(agents,showIdle){const list=agents||[];return showIdle?0:list.filter(agentIsIdle).length}
function idleButtonLabel(showIdle,count){return showIdle?'i hide idle':'i show idle'+(count?' ('+count+')':'')}
"""


# The palette both pages share. Themes pick the final colors so the accents
# read on light and dark backgrounds alike; the board page reuses the same
# variables rather than carrying a second copy that could drift.
PANEL_THEME_CSS = r""":root{color-scheme:light dark;--bg:#20242c;--panel:#292e38;--surface:#242a33;--surface-low:#1d222a;--control:#303745;--header:#20242cf2;--line:#465064;--text:#edf2f7;--muted:#9da8b9;--navigation:#66b3ff;--selection:#66b3ff;--identity:#d58cff;--success:#59d98e;--attention:#f3c969;--failure:#ff6b72;--idle:#9da8b9;--unknown:#9da8b9}
@media(prefers-color-scheme:light){:root{--bg:#f6f8fa;--panel:#fff;--surface:#f6f8fa;--surface-low:#eef1f4;--control:#fff;--header:#f6f8faf2;--line:#8c959f;--text:#1f2328;--muted:#57606a;--navigation:#075ea8;--selection:#075ea8;--identity:#6f42c1;--success:#167342;--attention:#805b10;--failure:#b42318;--idle:#57606a;--unknown:#57606a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}
header{position:sticky;top:0;z-index:5;padding:10px 12px;background:var(--header);border-bottom:1px solid var(--line);backdrop-filter:blur(8px)}
.brand{color:var(--navigation);font-weight:800;letter-spacing:.08em}.nav{color:var(--navigation);font-weight:700}.status{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}.chip{padding:2px 7px;border:1px solid var(--line);border-radius:999px;color:var(--muted)}
.controls{display:flex;gap:5px;flex-wrap:wrap;margin-top:8px}button{background:var(--control);color:var(--text);border:1px solid var(--line);border-radius:5px;padding:4px 7px;font:inherit;cursor:pointer}button.active{color:var(--selection);border-color:var(--selection);font-weight:800}"""


PANEL_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Side Dog</title>
<style>
""" + PANEL_THEME_CSS + r"""
.chip.clean,.chip.merged{color:var(--success);border-color:currentColor}.chip.pending,.chip.partial{color:var(--attention);border-color:currentColor}.chip.failed{color:var(--failure);border-color:currentColor}.chip.open{color:var(--navigation);border-color:currentColor}.chip.unknown{color:var(--unknown);border-color:currentColor}
.view-notice{margin-top:8px;padding:7px 9px;border:1px solid var(--line);border-radius:5px;background:var(--surface-low);font-weight:700}.view-notice[hidden]{display:none}
#roots{display:grid;grid-template-columns:1fr;gap:10px;padding:10px;overflow-x:auto}.root{min-width:0;border:1px solid var(--line);border-radius:8px;background:var(--panel);overflow:hidden}.root-head{padding:8px 10px;border-bottom:1px solid var(--line)}
.root-title{font-weight:800;color:var(--identity)}.agents{display:flex;flex-direction:column;gap:3px;margin-top:6px}.agent{font-size:12px;color:var(--muted);padding:2px 5px;background:var(--surface-low);border-radius:4px;overflow-wrap:anywhere}.agent-name{color:var(--identity);font-weight:800}.agent-worktree{color:var(--text);font-weight:700}.agent-purpose{color:var(--text)}.agent-meta{color:var(--muted)}.agent-state{font-weight:800}.agent-state.success,.state-mark.success{color:var(--success)}.agent-state.running,.state-mark.running,.agent-state.warning,.state-mark.warning{color:var(--attention)}.agent-state.failed,.state-mark.failed{color:var(--failure)}.agent-state.idle,.state-mark.idle{color:var(--idle)}.agent-state.unknown,.state-mark.unknown{color:var(--unknown)}
.usage{margin-top:6px;color:var(--muted);font-size:12px}.usage summary{cursor:pointer}.usage-row{padding:2px 0 0 12px}
.timeline{padding:5px}.unit{margin:5px 0;padding:7px 8px;border-left:3px solid var(--line);background:var(--surface);border-radius:4px}.unit time{color:var(--muted)}.unit a{color:inherit;text-decoration:none}.unit a:hover{text-decoration:underline}.unit.failed{border-color:var(--failure)}.unit.success{border-color:var(--success)}.unit.running,.unit.warning{border-color:var(--attention)}.unit.idle,.unit.unknown{border-color:var(--line)}.state-mark{display:inline-block;min-width:1.25em;font-weight:900}.state-mark.success{color:var(--success)}.state-mark.running,.state-mark.warning{color:var(--attention)}.state-mark.failed{color:var(--failure)}.state-mark.idle{color:var(--idle)}.state-mark.unknown{color:var(--unknown)}
.summary{font-weight:700}.detail{color:var(--muted);overflow-wrap:anywhere}.stages{color:var(--navigation);margin-top:3px}.day{margin:10px 0 5px;color:var(--navigation);border-bottom:1px solid var(--line)}details>summary{cursor:pointer}.empty{padding:15px;color:var(--muted)}
.highway-shell{padding:8px}.highway-score{display:flex;justify-content:space-between;color:var(--muted);margin-bottom:5px}.combo{color:var(--success);font-weight:800}.highway{--now-line:30px;position:relative;height:270px;overflow:hidden;border:1px solid var(--line);border-radius:6px;background:var(--surface-low)}.lane-grid{position:absolute;inset:0;display:grid;grid-template-columns:repeat(4,1fr)}.lane{border-left:1px solid color-mix(in srgb,var(--line) 40%,transparent);text-align:center;color:var(--muted);font-size:11px;padding-top:5px}.lane:first-child{border-left:0}.receptor{position:absolute;left:0;right:0;top:var(--now-line);border-top:2px solid var(--navigation);box-shadow:0 0 8px color-mix(in srgb,var(--navigation) 35%,transparent)}.receptor::after{content:'NOW';position:absolute;right:4px;top:2px;color:var(--navigation);font-size:10px}.highway-note{position:absolute;z-index:2;top:calc(var(--now-line) + var(--y));left:calc(var(--lane) * 25% + 12.5% - 8px + var(--offset));width:16px;height:16px;border:2px solid var(--unknown);border-radius:50%;background:var(--bg);color:var(--unknown)}.highway-note[hidden]{display:none}.highway-note.success{border-color:var(--success);color:var(--success)}.highway-note.failed{border-color:var(--failure);color:var(--failure)}.highway-note.running{border-color:var(--attention);color:var(--attention)}.highway-note.fresh{box-shadow:0 0 12px currentColor}.highway-note:not(.show-judgment) .judgment{display:none}.highway-note .judgment{position:absolute;left:20px;top:-2px;font-size:9px;font-weight:800;white-space:nowrap}.highway-note .hold{position:absolute;left:4px;top:14px;width:4px;height:var(--hold);min-height:0;background:currentColor;border-radius:2px;opacity:.65}.highway-note a{position:absolute;inset:-4px}
body.columns #roots{grid-template-columns:repeat(var(--count),minmax(300px,1fr))}body.stack #roots{grid-template-columns:1fr}body.paused::after{content:"… PAUSED";position:fixed;right:12px;bottom:12px;color:var(--attention);background:var(--bg);border:1px solid currentColor;padding:5px 8px;border-radius:5px;font-weight:800}
@media(max-width:620px){header{position:static}.controls button{flex:1}body.columns #roots{grid-template-columns:1fr}}
</style></head><body class="auto"><header><div><span class="brand">SIDE DOG</span> <span id="connection">connecting…</span> · <a class="nav" href="board" title="Every live coding-agent session on this machine">board</a></div><div id="summary" class="status"></div><div class="controls">
<button data-layout="auto" class="active">auto</button><button data-layout="columns">columns</button><button data-layout="stack">stack</button><button id="highway">h highway</button><button id="speed">s 1×</button><button id="expand">e expand</button><button id="filter">f all</button><button id="filesystem" title="Toggle background activity">F show background</button><button id="pause">p pause</button><button id="reverse">r oldest</button><button id="all">a all</button><button id="idle">i show idle</button>
</div><div id="notice" class="view-notice" role="status" aria-live="polite" aria-atomic="true" hidden></div></header><main id="roots"></main>
<script>
""" + PANEL_HIGHWAY_LOGIC_JS + r"""
const motionQuery=window.matchMedia('(prefers-reduced-motion: reduce)');
const state={roots:[],units:new Map(),mode:{label:'starting discovery',compact:'starting'},expanded:false,filter:'all',paused:false,newest:true,showIdle:false,showFilesystemActivity:false,layout:'auto',focus:null,queued:[],view:'timeline',speed:1,motionReduced:motionQuery.matches,frozenAt:motionQuery.matches?Date.now():null};
const NOTICE_MS=2000;let noticeTimer=null;
const ROOT_MIN_PX=300,ROOT_PADDING_PX=20,ROOT_GAP_PX=10;
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const githubKlass=v=>{v=String(v||'').toLowerCase();if(v.includes('fail')||v.includes('block')||v.includes('error')||v.includes('conflict'))return'failed';if(v.includes('clean')||v.includes('success')||v.includes('merge'))return'clean';if(v.includes('pend')||v.includes('partial')||v.includes('running'))return'pending';if(v.includes('open'))return'open';return'unknown'};
function day(u){const d=new Date((u.epoch||0));return Number.isNaN(+d)?'Unknown':d.toLocaleDateString([],{weekday:'short',month:'short',day:'numeric'});}
function unitHTML(u){const e=u.events?.[u.events.length-1]||{};const status=semanticStatus(e.status).role;const marker=statusMarker(e.status);const link=u.url?`<a href="${esc(u.url)}" target="_blank" rel="noopener">`:'<span>';const close=u.url?'</a>':'</span>';
 if(u.type==='filesystem_burst'){const s=u.summary||{};const paths=(s.paths||[]).map(p=>`${esc(p[0])}${p[1]>1?' ×'+p[1]:''}`).join(' · ');return `<details class="unit ${status}" ${state.expanded?'open':''}><summary><time>${when(u)}</time> ${marker}<b>Files · ${s.changes||0} changed${s.removals?' · '+s.removals+' removed':''} · ${(s.paths||[]).length} paths${(s.lines_added||s.lines_removed)?' · +'+(s.lines_added||0)+'/-'+(s.lines_removed||0):''}</b></summary><div class="detail">${paths}</div></details>`}
 if(u.type==='pipeline')return `<article class="unit ${status}"><div>${link}<time>${when(u)}</time> ${marker}<span class="summary">${esc(u.title||'Agent task')}</span>${close}</div><div class="stages">${(u.stages||[]).map(esc).join(' → ')}</div></article>`;
 return `<article class="unit ${status}">${link}<time>${eventWhen(u,e)}</time> ${marker}<span class="summary">${esc(eventText(e))}</span>${close}</article>`;}
function highwayMarkHTML(mark){const lane=HIGHWAY_LANES.indexOf(mark.lane);const fresh=mark.y<8?' fresh':'';const judgment=mark.showJudgment?' show-judgment':'';const hold=mark.status==='running'?`<span class="hold"></span>`:'';const link=mark.url?`<a href="${esc(mark.url)}" target="_blank" rel="noopener" aria-label="${esc(mark.detail)}"></a>`:'';return`<span class="highway-note ${mark.status}${fresh}${judgment}" data-mark-id="${esc(mark.id)}" style="--lane:${lane};--y:${mark.y}px;--hold:${mark.hold}px;--offset:${mark.offset}px" title="${esc(mark.detail)}" aria-label="${esc(mark.detail)}">${hold}<span class="judgment">${mark.judgment}</span>${link}</span>`}
function highwayHTML(root,nowMs){const snapshot=highwaySnapshot([...state.units.values()],root.id,nowMs,state.speed,state.filter,state.showFilesystemActivity);const lanes=HIGHWAY_LANES.map(lane=>`<div class="lane">${lane}</div>`).join('');return`<div class="highway-score"><span>pulse · ${state.speed}× · unknown stays neutral</span><span class="combo">combo ${snapshot.combo}</span></div><div class="highway" aria-label="Live activity highway for ${esc(root.name)}"><div class="lane-grid">${lanes}</div><div class="receptor"></div>${snapshot.marks.map(highwayMarkHTML).join('')}</div>`}
function visibleUnits(root){let xs=[...state.units.values()].filter(u=>u.root===root.id).filter(u=>state.showFilesystemActivity||(u.events||[]).some(event=>backgroundEventAllowed(event,false)));if(state.filter==='milestones')xs=xs.filter(u=>u.type==='pipeline'||['test','commit','push','pr','merge','issue','github','branch','worktree','session'].includes(u.events?.[0]?.kind));if(state.filter==='files')xs=xs.filter(u=>u.type==='filesystem_burst'||['file','config'].includes(u.events?.[0]?.kind));xs.sort((a,b)=>(a.epoch-b.epoch)||(a.id>b.id?1:-1));if(state.newest)xs.reverse();return xs;}
function pricingAge(ms){const seconds=Math.max(0,Math.floor((Date.now()-Number(ms||0))/1000));if(seconds<60)return `${seconds}s old`;const minutes=Math.floor(seconds/60);return minutes<60?`${minutes}m old`:`${Math.floor(minutes/60)}h old`}
function pricingHTML(pricing){const entries=Object.entries(pricing||{});if(!entries.length)return'';return `<div class="usage-row">Pricing · ${entries.map(([name,value])=>`${esc(name)} ${esc(value.source)} (<span data-pricing-age="${Number(value.captured_epoch_ms||0)}">${pricingAge(value.captured_epoch_ms)}</span>)`).join(' · ')}</div>`}
function refreshPricingAges(){document.querySelectorAll('[data-pricing-age]').forEach(node=>{node.textContent=pricingAge(node.dataset.pricingAge)})}
function usageHTML(usage){if(!usage?.label)return'';const lines=(usage.lines||[usage.label]).map((line,index)=>index?`<div class="usage-row">${esc(line)}</div>`:'').join('');const rows=(usage.rows||[]).map(row=>`<div class="usage-row">${esc(row.label)} · ${esc(row.status)} · ${esc(row.model||'model ?')} · today ${Number(row.today_tokens||0).toLocaleString()} tok${row.today_cost_usd===undefined?'':` / API est $${Number(row.today_cost_usd).toFixed(2)}`} · lifetime ${Number(row.lifetime_tokens||0).toLocaleString()} tok${row.lifetime_cost_usd===undefined?'':` / API est $${Number(row.lifetime_cost_usd).toFixed(2)}`}${row.last_activity?` · ${esc(row.last_activity)}`:''}</div>`).join('');const note='<div class="usage-row">API estimate = public list prices applied to local logs; not a subscription bill</div>';return `<details class="usage" ${state.expanded?'open':''}><summary>${esc(usage.label)}</summary>${lines}${rows}${note}${pricingHTML(usage.pricing)}</details>`}
function panelSurface(a){const explicit=String(a?.surface||'').trim();if(explicit)return explicit;const label=String(a?.label||'').trim();return label.toLowerCase().startsWith('codex desktop')?'Codex Desktop':''}
function panelTask(a){const label=String((a?.task??a?.label)||'').trim();const surface=panelSurface(a);if(!label)return'unidentified';if(surface&&label.toLowerCase().startsWith(surface.toLowerCase()))return label.slice(surface.length).replace(/^\s*[·:–-]\s*/,'').trim()||'purpose not named';return label}
function panelAgentHTML(a,branch){const surface=panelSurface(a);const task=panelTask(a);const worktree=String(a?.worktree||a?.branch||branch||'').trim();const meta=[a?.model||'model ?',a?.effort||'effort ?',surface?`[${surface}]`:'' ].filter(Boolean).join('/').replace('/[',' [');return `<span class="agent"><span class="agent-worktree">${esc(worktree||'worktree')}</span> · <span class="agent-name">${esc(a.agent||'agent')}</span> · <span class="agent-purpose">${esc(task)}</span> · <span class="agent-meta">${esc(meta)}</span> · ${agentStatusHTML(a.status)}</span>`}
function rootHTML(root){const g=root.git||{},p=root.github||{};const allAgents=root.agents||[];const agents=visibleAgents(allAgents,state.showIdle).map(a=>panelAgentHTML(a,g.branch)).join('');let lastDay='';const units=visibleUnits(root);const rows=units.map(u=>{const d=day(u);const marker=d!==lastDay?(lastDay=d,`<div class="day">${esc(d)}</div>`):'';return marker+unitHTML(u)}).join('');const content=state.view==='highway'?`<div class="highway-shell" data-root="${esc(root.id)}">${highwayHTML(root,state.frozenAt||Date.now())}</div>`:`<div class="timeline">${rows||'<div class="empty">waiting for agent activity…</div>'}</div>`;const git=g.branch?`Git ${esc(g.branch)} @ ${esc(g.short_oid||'?')}`:'No Git repository';return `<section class="root" data-root="${esc(root.id)}"><div class="root-head"><div class="root-title">Watching: ${esc(root.name)}</div><div class="detail">${git}</div><div class="status">${p.number?`<span class="chip ${githubKlass(p.state)}">PR #${p.number} ${esc(p.state)}</span>`:''}${p.ci?`<span class="chip ${githubKlass(p.ci)}">${esc(p.ci)}</span>`:''}</div><div class="agents">${agents||(allAgents.length?'':'<span class="agent">? no active agent</span>')}</div>${usageHTML(root.usage)}</div>${content}</section>`;}
function columnsFit(){const count=Math.max(1,state.roots.length);return innerWidth>=ROOT_PADDING_PX+count*ROOT_MIN_PX+Math.max(0,count-1)*ROOT_GAP_PX}
function effectiveLayout(){if(state.focus)return'stack';if(state.layout==='stack')return'stack';if(state.layout==='columns')return'columns';return columnsFit()?'columns':'stack'}
function bodyClass(){return effectiveLayout()+(state.paused?' paused':'')+(highwayShouldAnimate(state.view,state.paused,state.motionReduced)?' highway-live':'')}
let highwayFrame=null,lastHighwayFrame=0;
function renderHighways(nowMs){document.querySelectorAll('.highway-shell').forEach(shell=>{const root=state.roots.find(item=>item.id===shell.dataset.root);if(!root)return;const marks=new Map(highwaySnapshot([...state.units.values()],root.id,nowMs,state.speed,state.filter,state.showFilesystemActivity).marks.map(mark=>[mark.id,mark]));shell.querySelectorAll('.highway-note').forEach(note=>{const mark=marks.get(note.dataset.markId);note.hidden=!mark;if(!mark)return;note.style.setProperty('--y',`${mark.y}px`);note.style.setProperty('--hold',`${mark.hold}px`);note.style.setProperty('--offset',`${mark.offset}px`);note.classList.toggle('fresh',mark.y<8);note.classList.toggle('show-judgment',mark.showJudgment)})})}
function highwayTick(timestamp){if(!highwayShouldAnimate(state.view,state.paused,state.motionReduced)){highwayFrame=null;return}if(timestamp-lastHighwayFrame>=80){lastHighwayFrame=timestamp;renderHighways(Date.now())}highwayFrame=requestAnimationFrame(highwayTick)}
function syncHighwayAnimation(){const animate=highwayShouldAnimate(state.view,state.paused,state.motionReduced);if(!animate&&highwayFrame!==null){cancelAnimationFrame(highwayFrame);highwayFrame=null}if(animate&&highwayFrame===null)highwayFrame=requestAnimationFrame(highwayTick)}
function renderResponsiveChrome(){document.body.className=bodyClass();document.querySelector('#summary').innerHTML=`<span class="chip">Mode: ${esc(innerWidth<480?(state.mode.compact||state.mode.label):state.mode.label)}</span><span class="chip">Watching ${state.roots.length} folder${state.roots.length===1?'':'s'}</span>`+state.roots.map(r=>`<span class="chip">${esc(r.name)}</span>`).join('')}
function render(){renderResponsiveChrome();document.documentElement.style.setProperty('--count',Math.max(1,state.focus?1:state.roots.length));const roots=state.focus?state.roots.filter(r=>r.id===state.focus):state.roots;const idleTotal=state.roots.reduce((n,r)=>n+hiddenIdleCount(r.agents,false),0);document.querySelector('#idle').textContent=idleButtonLabel(state.showIdle,idleTotal);document.querySelector('#filesystem').textContent=`F ${state.showFilesystemActivity?'hide':'show'} background`;document.querySelector('#roots').innerHTML=roots.map(rootHTML).join('');document.querySelectorAll('[data-layout]').forEach(b=>b.classList.toggle('active',b.dataset.layout===state.layout));syncHighwayAnimation()}
function apply(message){if(state.paused){state.queued.push(message);return}if(message.type==='snapshot'){state.roots=message.roots||[];state.units=new Map((message.units||[]).map(u=>[u.id,u]));state.mode=message.discovery_mode||state.mode;state.showFilesystemActivity=message.display?.show_filesystem_activity===true;}else if(message.type==='unit'){state.units.set(message.unit.id,message.unit)}else if(message.type==='banner'){const i=state.roots.findIndex(r=>r.id===message.root.id);if(i>=0)state.roots[i]=message.root;else state.roots.push(message.root)}else if(message.type==='display'){state.showFilesystemActivity=message.show_filesystem_activity===true}render();}
const es=new EventSource('events');es.addEventListener('snapshot',e=>{document.querySelector('#connection').textContent='live';apply(JSON.parse(e.data))});es.addEventListener('unit',e=>apply({type:'unit',unit:JSON.parse(e.data)}));es.addEventListener('banner',e=>apply({type:'banner',root:JSON.parse(e.data)}));es.addEventListener('display',e=>apply({type:'display',...JSON.parse(e.data)}));es.onerror=()=>document.querySelector('#connection').textContent='reconnecting…';
setInterval(()=>{if(!state.paused)refreshPricingAges()},1000);
function showNotice(message){const notice=document.querySelector('#notice');notice.textContent=`View changed — ${message}`;notice.hidden=false;if(noticeTimer!==null)clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>{notice.hidden=true;notice.textContent='';noticeTimer=null},NOTICE_MS)}
function layoutNotice(layout){if(state.focus){const root=state.roots.find(r=>r.id===state.focus);return`Showing only ${root?.name||'the selected folder'} — it stays full-width; the ${layout} layout returns when all folders are shown.`}if(layout==='auto')return'Automatic layout — folders use columns when each has at least 300 pixels; otherwise they stack.';if(layout==='columns'&&!columnsFit())return'Columns view — the pane is too narrow to fit every folder, so the row scrolls sideways.';if(layout==='columns')return'Columns view — each folder has its own side-by-side list.';return'Stacked view — each folder has its own full-width list.'}
function allRootsNotice(){return effectiveLayout()==='columns'?'All folders — one column each.':'All folders — stacked one above the other.'}
function setLayout(layout){state.layout=layout;render();showNotice(layoutNotice(layout))}
function toggleHighway(){state.view=state.view==='timeline'?'highway':'timeline';document.querySelector('#highway').textContent=`h ${state.view==='highway'?'timeline':'highway'}`;render();showNotice(state.view==='highway'?(state.motionReduced?'Pulse score — reduced motion keeps the four-lane activity strip static.':'Live highway — events fall from NOW through files, tests, git, and GitHub lanes.'):timelineOrderNotice(state.newest))}
function cycleHighwaySpeed(){const speeds=[0.5,1,2];state.speed=speeds[(speeds.indexOf(state.speed)+1)%speeds.length];document.querySelector('#speed').textContent=`s ${state.speed}×`;render();showNotice(`Highway speed — ${state.speed}×; time still maps linearly to distance.`)}
function toggleExpanded(){state.expanded=!state.expanded;document.querySelector('#expand').textContent=`e ${state.expanded?'compact':'expand'}`;render();showNotice(state.expanded?'Expanded — grouped file paths are open.':'Compact — grouped file paths are closed.')}
function cycleFilter(){state.filter={all:'milestones',milestones:'files',files:'all'}[state.filter];document.querySelector('#filter').textContent=`f ${state.filter}`;render();showNotice({milestones:'Milestones only — commits, pushes, PRs, tests, branches.',files:'File writes only — everything else is hidden.',all:'Everything — file writes and milestones together.'}[state.filter])}
function toggleFilesystemActivity(){state.showFilesystemActivity=!state.showFilesystemActivity;fetch('display',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({show_filesystem_activity:state.showFilesystemActivity})}).catch(()=>{});render();showNotice(state.showFilesystemActivity?'Background activity visible — files and lifecycle rows included':'Background activity hidden')}
function togglePause(){state.paused=!state.paused;state.frozenAt=highwayFreezeTimestamp(state.paused,state.motionReduced,state.frozenAt,Date.now());document.querySelector('#pause').textContent=`p ${state.paused?'resume':'pause'}`;if(!state.paused){const q=state.queued.splice(0);q.forEach(apply)}render();showNotice(state.paused?'Paused — collection continues; display updates are held.':'Live — held updates are now visible.')}
function toggleOrder(){state.newest=!state.newest;document.querySelector('#reverse').textContent=`r ${state.newest?'oldest':'newest'}`;render();showNotice(state.newest?'Newest first — new events appear at the top.':'Oldest first — new events appear at the bottom.')}
function showAllRoots(){state.focus=null;render();showNotice(allRootsNotice())}
function toggleIdle(){state.showIdle=!state.showIdle;render();showNotice(state.showIdle?'Idle agents — showing every session, idle ones included.':'Idle agents — idle sessions are hidden; working and unknown sessions stay visible.')}
function focusRoot(index){const root=state.roots[index];if(!root)return;state.focus=root.id;render();showNotice(`Showing only ${root.name}.`)}
function cycleRoot(){if(!state.roots.length)return;const index=state.focus?state.roots.findIndex(r=>r.id===state.focus):-1;focusRoot((index+1)%state.roots.length)}
document.querySelectorAll('[data-layout]').forEach(b=>b.onclick=()=>setLayout(b.dataset.layout));document.querySelector('#highway').onclick=toggleHighway;document.querySelector('#speed').onclick=cycleHighwaySpeed;document.querySelector('#expand').onclick=toggleExpanded;document.querySelector('#filter').onclick=cycleFilter;document.querySelector('#filesystem').onclick=toggleFilesystemActivity;document.querySelector('#pause').onclick=togglePause;document.querySelector('#reverse').onclick=toggleOrder;document.querySelector('#all').onclick=showAllRoots;document.querySelector('#idle').onclick=toggleIdle;
window.addEventListener('keydown',e=>{if(e.ctrlKey||e.metaKey||e.altKey)return;if(e.key==='h')toggleHighway();else if(e.key==='s')cycleHighwaySpeed();else if(e.key==='e')toggleExpanded();else if(e.key==='f')cycleFilter();else if(e.key==='F')toggleFilesystemActivity();else if(e.key==='p')togglePause();else if(e.key==='r')toggleOrder();else if(e.key==='i')toggleIdle();else if(e.key==='a')showAllRoots();else if(e.key==='Tab'){e.preventDefault();cycleRoot()}else if(/^[1-9]$/.test(e.key))focusRoot(Number(e.key)-1);else return});
motionQuery.addEventListener('change',event=>{state.motionReduced=event.matches;state.frozenAt=highwayFreezeTimestamp(state.paused,event.matches,state.frozenAt,Date.now());render();showNotice(event.matches?'Reduced motion — the pulse score is static and no animation frames run.':'Motion enabled — live highway movement is available.')});
window.addEventListener('resize',renderResponsiveChrome);
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
        nested = _http_url(github["url"])
        if nested:
            return nested
    explicit = event.get("url")
    if isinstance(explicit, str):
        validated = _http_url(explicit)
        if validated:
            return validated
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


def _http_url(value: str) -> str:
    """Keep only absolute HTTP(S) links safe for a browser href."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    if any(character.isspace() or ord(character) < 32 for character in value):
        return ""
    return value


def _safe_event(event: dict[str, Any], web_root: str) -> dict[str, Any]:
    safe = {key: event[key] for key in ALLOWED_EVENT_FIELDS - {"url"} if key in event}
    github = safe.get("github")
    if isinstance(github, dict) and "url" in github:
        github = dict(github)
        nested_url = github.pop("url")
        if isinstance(nested_url, str) and (validated := _http_url(nested_url)):
            github["url"] = validated
        safe["github"] = github
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
    github_query_branch: str | None = None
    github_refresh_branch: str | None = None
    last_agent_refresh: float = 0.0
    last_github_refresh: float = float("-inf")
    identities: dict[str, dict[str, str]] = field(default_factory=dict)
    git: dict[str, str] = field(default_factory=dict)
    usage_sessions: set[tuple[str, str]] = field(default_factory=set)
    usage_contexts: dict[tuple[str, str], dict[str, str]] = field(
        default_factory=dict
    )


class PanelFeed:
    def __init__(
        self,
        roots: Iterable[Path],
        follow_worktrees: bool = True,
        *,
        follow_herdr: bool = False,
        workspace_id: str | None = None,
        requested_roots: Iterable[Path] | None = None,
        discovery_mode: DiscoveryMode | None = None,
        poll_coordinator: PollCoordinator | None = None,
        notify: bool = True,
    ) -> None:
        self._lock = threading.Lock()
        self._notify = notify
        self.show_filesystem_activity = configured_filesystem_activity()
        self.roots: list[PanelRoot] = []
        self._labels: dict[str, int] = {}
        requested = list(roots)
        self._requested = set(requested if requested_roots is None else requested_roots)
        # Every configured pin, even one that also arrived through Herdr or
        # discovery: the pin guarantee must outlive how the folder got here.
        self._pinned = set(pinned_folders())
        self._follow_worktrees = follow_worktrees
        self._follow_herdr = follow_herdr
        self._workspace_id = workspace_id
        self.discovery_mode = discovery_mode or folder_discovery_mode(
            explicit_roots=bool(self._requested),
            follow_herdr=follow_herdr,
            require_herdr=False,
            workspace_only=workspace_id is not None,
        )
        self._discovering = self.discovery_mode.key == "automatic"
        self._herdr_error: str | None = None
        self._last_worktree_scan = 0.0
        for root in requested + sorted(self._pinned - set(requested)):
            self.roots.append(self._panel_root(root))
        self._unit_fingerprints: dict[str, str] = {}
        self._banner_fingerprints: dict[str, str] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max(2, watch_root_limit() * 2),
            thread_name_prefix="side-dog-panel",
        )
        self._poll_coordinator = poll_coordinator or create_poll_coordinator()
        self._usage_monitor = UsageMonitor()
        self._usage_monitor.tick()

    def _panel_root(self, root: Path) -> PanelRoot:
        label = root.name
        self._labels[label] = self._labels.get(label, 0) + 1
        if self._labels[label] > 1:
            label = f"{label}:{self._labels[label]}"
        path = events_path(root)
        startup = load_startup_history(root, path, reader=read_new_events)
        records = list(startup.records)
        github_record = startup.latest_github
        github = (
            dict(github_record["github"])
            if github_record is not None
            and isinstance(github_record.get("github"), dict)
            else None
        )
        return PanelRoot(
            root=root,
            label=label,
            path=path,
            position=startup.position,
            records=deque(records[-500:], maxlen=500),
            web_root=_github_web_root(root),
            github=github,
            github_branch=str((github or {}).get("branch") or "") or None,
            git=load_git_state(root) or {},
            usage_sessions=set(startup.usage_sessions),
        )

    def set_show_filesystem_activity(self, show: bool) -> bool:
        with self._lock:
            self.show_filesystem_activity = bool(show)
            return self.show_filesystem_activity

    def _display_wire(self) -> dict[str, bool]:
        return {"show_filesystem_activity": self.show_filesystem_activity}

    def _refresh_git_states(self) -> None:
        for state in self.roots:
            state.git = load_git_state(state.root) or {}

    def _follow_worktree_changes(self, now: float) -> bool:
        """Adopt worktrees that wake up and drop the ones that finish.

        The terminal does this every few seconds; a panel left open all day
        should not be stuck with the folder list it started with.
        """
        if (
            not self._follow_worktrees
            and not self._follow_herdr
            and not self._discovering
        ) or (
            now - self._last_worktree_scan < 5.0
        ):
            return False
        self._last_worktree_scan = now
        watched = [state.root for state in self.roots]
        changed = False
        live_order: list[Path] = []
        session_retired: list[Path] = []
        session_additions: list[Path] = []
        limit = watch_root_limit()
        if self._discovering:
            configuration = load_config()
            self._pinned = set(pinned_folders(configuration))
            session_retired, session_additions = rediscovered_roots(
                self.roots,
                configuration,
                limit,
                self._requested | self._pinned,
            )
            live_order = list(agent_working_folders())
        if self._follow_herdr:
            live_order, error = herdr_session_roots(self._workspace_id)
            if error and error != self._herdr_error:
                print(
                    f"side-dog: {error}; keeping current folders and retrying",
                    file=sys.stderr,
                )
            self._herdr_error = error
            session_retired, session_additions = reconcile_herdr_roots(
                watched, live_order, self._requested | self._pinned, limit
            )
        cycle_inventories = (
            build_worktree_inventories([*watched, *session_additions])
            if self._follow_worktrees
            else {}
        )
        current_branches = (
            inventory_branch_names(cycle_inventories)
            if self._follow_worktrees
            else None
        )
        additions = list(session_additions)
        if self._follow_worktrees:
            additions.extend(
                busy_worktrees(
                    watched,
                    int(time.time() * 1000),
                    limit,
                    live=set(live_order) if self._follow_herdr else None,
                    inventories=cycle_inventories,
                )
            )
        if self._workspace_id is not None:
            additions = [root for root in additions if root in set(live_order)]
        additions = list(dict.fromkeys(additions))
        room = max(0, limit - (len(watched) - len(session_retired)))
        for root in additions[:room]:
            if root not in watched:
                self.roots.append(self._panel_root(root))
                watched.append(root)
                changed = True
        finished = [
            state.root
            for state in self.roots
            if state.root not in self._requested
            and state.root not in self._pinned
            and state.root not in live_order
            and (
                folder_is_finished(
                    state.root,
                    current_branch=current_branches.get(state.root, ""),
                )
                if current_branches is not None and state.root in current_branches
                else folder_is_finished(state.root)
            )
        ]
        finished = keep_one_root(
            list(dict.fromkeys([*session_retired, *finished])), len(self.roots)
        )
        if finished:
            self.roots = [
                state for state in self.roots if state.root not in finished
            ]
            changed = True
        return changed

    @staticmethod
    def _agent_rows(
        identities: dict[str, dict[str, Any]], root: Path
    ) -> list[dict[str, str]]:
        unique: dict[str, AgentIdentity] = {}
        for identity in identities.values():
            identity_root = identity.get("root")
            if not isinstance(identity_root, str):
                continue
            try:
                if Path(identity_root).expanduser().resolve() != root.resolve():
                    continue
            except OSError:
                continue
            typed_identity = AgentIdentity.from_wire(identity)
            session_id = identity.get("session_id")
            key = typed_identity.pane_id or (
                agent_session_key(typed_identity.agent, session_id)
                if session_id
                else typed_identity.label or repr(identity)
            )
            unique[str(key)] = typed_identity
        return [
            {
                "agent": identity.agent,
                "label": identity.label,
                "task": (
                    identity.label[len("Codex Desktop") :]
                    .lstrip(" ·:–-")
                    or "purpose not named"
                    if identity.label.casefold().startswith("codex desktop")
                    else identity.label
                ),
                "model": display_model(identity.model),
                "effort": identity.effort,
                "status": identity.status.value,
                **(
                    {"surface": "Codex Desktop"}
                    if identity.label.casefold().startswith("codex desktop")
                    else {}
                ),
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
                    load_agent_identities, state.root
                )
            current_branch = state.git.get("branch")
            queried_branch = state.github_query_branch or state.github_branch
            if (
                queried_branch is not None
                and queried_branch != current_branch
            ):
                state.last_github_refresh = float("-inf")
            if state.github_refresh is None and (
                force
                or github_refresh_due(
                    state.github,
                    state.last_github_refresh,
                    now,
                    GITHUB_REFRESH_SECONDS,
                )
            ):
                state.last_github_refresh = now
                state.github_refresh_branch = current_branch
                state.github_query_branch = current_branch
                state.github_refresh = self._executor.submit(load_github_pr, state.root)

    def _collect_external_refreshes(self) -> bool:
        changed = False
        for state in self.roots:
            if state.agent_refresh is not None and state.agent_refresh.done():
                try:
                    identities = state.agent_refresh.result()
                    agents = self._agent_rows(identities, state.root)
                except Exception:
                    identities = {}
                    agents = []
                state.agent_refresh = None
                state.identities = identities
                state.usage_sessions.update(
                    usage_session_keys((), identities, state.root)
                )
                state.usage_contexts = refreshed_usage_contexts(
                    state.usage_contexts, identities, state.root
                )
                if agents != state.agents:
                    state.agents = agents
                    changed = True
            if state.github_refresh is not None and state.github_refresh.done():
                try:
                    github, github_error = state.github_refresh.result()
                except Exception as error:
                    github = None
                    github_error = str(error)
                state.github_refresh = None
                current_branch = state.git.get("branch")
                refresh_branch = state.github_refresh_branch
                state.github_refresh_branch = None
                if current_branch != refresh_branch:
                    state.last_github_refresh = float("-inf")
                    continue
                if github is None and not is_definitive_no_pr(github_error):
                    continue
                if github != state.github or refresh_branch != state.github_branch:
                    state.github = github
                    state.github_branch = refresh_branch
                    changed = True
        return changed

    def _wire_root(self, state: PanelRoot) -> dict[str, Any]:
        git = state.git
        branch = git.get("branch")
        agents = []
        for agent in state.agents or []:
            row = dict(agent)
            if branch and not row.get("branch"):
                row["branch"] = branch
            agents.append(row)
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
            "agents": agents,
            "usage": usage_summary_wire(
                self._usage_monitor.snapshot,
                state.usage_sessions,
                state.usage_contexts.values(),
                session_cadence=float(
                    self._usage_monitor.settings.get(
                        "session_refresh_seconds", 180.0
                    )
                ),
                block_cadence=float(
                    self._usage_monitor.settings.get("block_refresh_seconds", 10.0)
                ),
                include_pricing_age=False,
            ),
        }

    def _units(self) -> list[dict[str, Any]]:
        units: list[dict[str, Any]] = []
        for state in self.roots:
            records = _tagged(state.records, state.root, state.label)
            units.extend(
                wire_unit(unit, state.web_root)
                for unit in build_activity_units(
                    records,
                    expanded_history=False,
                    show_lifecycle=self.show_filesystem_activity,
                )
            )
        return units

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            # New roots have never been refreshed, so the normal due check
            # starts them immediately. Do not force another GitHub query every
            # time a browser asks for a fresh snapshot.
            self._refresh_git_states()
            self._usage_monitor.tick()
            self._start_external_refreshes(time.monotonic())
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
                "discovery_mode": self.discovery_mode.wire(),
                "display": self._display_wire(),
                "roots": roots,
                "units": units,
            }

    def poll(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            self._poll_coordinator.tick(
                PollTarget.from_wire(state.root, state.identities)
                for state in self.roots
            )
            self._refresh_git_states()
            changed = self._collect_external_refreshes()
            if self._usage_monitor.tick():
                changed = True
            roots_changed = self._follow_worktree_changes(time.monotonic())
            if roots_changed:
                changed = True
            for state in self.roots:
                records, state.position = read_new_events(state.path, state.position, state.root)
                if records:
                    state.usage_sessions.update(usage_session_keys(records, {}))
                    state.records.extend(records)
                    if any(
                        record.get("kind") in {"pr", "merge"}
                        for record in records
                    ):
                        state.last_github_refresh = float("-inf")
                    if self._notify:
                        for record in records:
                            notify_for_event(state.label, record)
                    changed = True
            updates: list[tuple[str, dict[str, Any]]] = []
            if changed:
                units = self._units()
                current_fingerprints = {
                    unit["id"]: _json_fingerprint(unit) for unit in units
                }
                removed = self._unit_fingerprints.keys() - current_fingerprints.keys()
                if roots_changed or removed:
                    self._unit_fingerprints = current_fingerprints
                    roots = [self._wire_root(state) for state in self.roots]
                    self._banner_fingerprints = {
                        root["id"]: _json_fingerprint(root) for root in roots
                    }
                    updates.append(
                        (
                            "snapshot",
                            {
                                "schema": PANEL_SCHEMA,
                                "type": "snapshot",
                                "generated_at": datetime.now().astimezone().isoformat(),
                                "discovery_mode": self.discovery_mode.wire(),
                                "display": self._display_wire(),
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
            self._start_external_refreshes(now)
            for state in self.roots:
                root = self._wire_root(state)
                fingerprint = _json_fingerprint(root)
                if self._banner_fingerprints.get(root["id"]) != fingerprint:
                    self._banner_fingerprints[root["id"]] = fingerprint
                    updates.append(("banner", root))
            return updates

    def close(self) -> None:
        self._poll_coordinator.close(wait=False)
        self._usage_monitor.close()
        self._executor.shutdown(wait=False, cancel_futures=True)


BOARD_EVENT = "board"

# The board page's pure logic, kept apart from the DOM so a test can run it
# under Node the way the highway logic is tested.
BOARD_LOGIC_JS = r"""
const BOARD_GROUPS=['none','surface','repo'];
function boardGroup(query,configured){const value=String(query||'').trim();if(BOARD_GROUPS.includes(value))return value;return BOARD_GROUPS.includes(configured)?configured:'none'}
function resolveGroup(current,query,message){if(current!==null&&current!==undefined)return current;const value=String(query||'').trim();if(BOARD_GROUPS.includes(value))return value;if(message&&message.discovering)return null;return boardGroup('',message?.group)}
function nextBoardGroup(group){const index=BOARD_GROUPS.indexOf(group);return BOARD_GROUPS[(index+1)%BOARD_GROUPS.length]}
function formatAge(seconds){if(seconds===null||seconds===undefined||Number.isNaN(Number(seconds)))return'';const s=Math.max(0,Math.floor(Number(seconds)));if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m';if(s<86400)return Math.floor(s/3600)+'h';return Math.floor(s/86400)+'d'}
function liveAge(row,snapshotEpochMs,nowMs){if(row.age_seconds===null||row.age_seconds===undefined)return null;const elapsed=Math.max(0,(Number(nowMs)-Number(snapshotEpochMs||nowMs))/1000);return Number(row.age_seconds)+elapsed}
function groupLabel(row,group){if(group==='surface')return row.surface||'unknown';if(group==='repo')return row.repository_label||row.repository||'no repository';return''}
function groupRank(row,group){if(group==='surface'){const surface=String(row.surface||'unknown');return[surface==='unknown'?1:0,surface.toLowerCase()]}if(group==='repo'){const repository=String(row.repository||'');return[repository?0:1,repository.toLowerCase(),String(row.repository_label||'')]}return[]}
function compareRanks(a,b){for(let i=0;i<Math.max(a.length,b.length);i+=1){if(a[i]===b[i])continue;return a[i]<b[i]?-1:1}return 0}
function groupedRows(rows,group){const indexed=(rows||[]).map((row,index)=>({row,index,rank:groupRank(row,group)}));indexed.sort((a,b)=>compareRanks(a.rank,b.rank)||(a.index-b.index));return indexed.map(item=>item.row)}
function boardSections(rows,group){const sections=[];let current=null;for(const row of groupedRows(rows,group)){const label=groupLabel(row,group);if(!current||current.label!==label){current={label,rows:[]};sections.push(current)}current.rows.push(row)}return sections}
function boardSummary(message){const sessions=Number(message?.sessions||0),repositories=Number(message?.repositories||0);let text=sessions+' session'+(sessions===1?'':'s');if(repositories)text+=' · '+repositories+' repo'+(repositories===1?'':'s');if(message?.discovering)text+=' · discovering';return text}
function repoCell(row,group){if(group==='repo')return String(row.branch||'');const repository=String(row.repository||''),branch=String(row.branch||'');return repository&&branch?repository+'  '+branch:(repository||branch)}
function webUrl(value){const text=String(value||'');return/^https?:\/\//i.test(text)?text:''}
function issueOverflow(row){const omitted=Number(row?.issues_omitted||0);return omitted>0?' +'+omitted:''}
"""

BOARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Side Dog board</title>
<style>
""" + PANEL_THEME_CSS + r"""
.conflicts{margin-top:8px;display:flex;flex-direction:column;gap:3px}.conflict{color:var(--attention);font-weight:700}.conflicts[hidden]{display:none}
main{padding:10px 12px;overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:640px}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}th{color:var(--muted);font-weight:700;font-size:12px;letter-spacing:.04em}
tr.group th{color:var(--text);font-weight:800;background:var(--surface-low);padding-top:9px}td.agent{color:var(--identity);font-weight:800}td a{color:inherit;text-decoration:none}td a:hover{text-decoration:underline}
td.status{white-space:nowrap;font-weight:800}tr.working td.status{color:var(--attention)}tr.blocked td.status{color:var(--failure)}tr.done td.status{color:var(--success)}tr.idle td.status,tr.unknown td.status{color:var(--idle)}
td.issue.confirmed{color:var(--navigation)}td.issue.inferred,td.pr.none,td.issue.none{color:var(--muted)}td.pr.passed{color:var(--success)}td.pr.failed,td.pr.changes{color:var(--failure)}td.pr.pending{color:var(--attention)}td.pr.closed{color:var(--muted)}
tr.detail td{color:var(--muted);font-size:12px;padding-top:0;border-bottom:1px solid var(--line)}tr.detail[hidden]{display:none}.empty{padding:15px;color:var(--muted)}.empty[hidden]{display:none}
@media(max-width:620px){header{position:static}.controls button{flex:1}}
</style></head><body><header><div><span class="brand">SIDE DOG</span> board <span id="connection">connecting…</span> · <a id="timeline" class="nav" href="./" title="The activity timeline">timeline</a></div><div id="summary" class="status"></div><div class="controls">
<button data-group="none">g flat</button><button data-group="surface">by surface</button><button data-group="repo">by repo</button><button id="detail">d hide detail</button>
</div><div id="conflicts" class="conflicts" role="status" aria-live="polite" hidden></div></header>
<main><table id="board" aria-label="Live coding-agent sessions"><thead><tr><th>AGENT</th><th id="surface-head">SURFACE</th><th id="repo-head">REPO / BRANCH</th><th>ISSUE</th><th>PR</th><th>STATUS</th></tr></thead><tbody id="rows"></tbody></table><div id="empty" class="empty" hidden>No coding-agent sessions found. Sessions appear here as Claude Code, Codex, and the other supported agents start working.</div></main>
<script>
""" + BOARD_LOGIC_JS + r"""
const base=location.pathname.replace(/\/board\/?$/,'');
document.querySelector('#timeline').href=base+'/';
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const query=new URLSearchParams(location.search).get('group');
const state={message:null,group:null,detail:true,detailChosen:false};
function prKlass(row){const text=String(row.pr_text||'');if(text==='—')return'none';if(/merged|closed/.test(text))return'closed';if(text.includes('✗rev'))return'changes';if(text.includes('✗ci'))return'failed';if(text.includes('…ci'))return'pending';if(text.includes('✓ci'))return'passed';return'none'}
function issueKlass(row){const issues=row.issues||[];if(!issues.length)return'none';return issues[0].confirmed?'confirmed':'inferred'}
function link(url,text){const safe=webUrl(url);return safe?`<a href="${esc(safe)}" target="_blank" rel="noopener">${esc(text)}</a>`:esc(text)}
function statusCell(row){const age=formatAge(liveAge(row,state.message?.epoch_ms,Date.now()));return `${esc(row.status_glyph||'?')} ${esc(row.status||'unknown')} <span data-age="${esc(row.id)}">${esc(age)}</span>`}
function issueLinks(row){return (row.issues||[]).map(issue=>link(issue.url,issue.label)).join(', ')+esc(issueOverflow(row))}
function issueCell(row){const issues=row.issues||[];if(!issues.length)return esc(row.issue_text||'—');return issueLinks(row)}
function rowHTML(row){const surface=currentGroup()==='surface'?'':`<td>${esc(row.surface)}</td>`;const issue=issueCell(row);return `<tr class="row ${esc(row.status)}" data-row="${esc(row.id)}"><td class="agent">${esc(row.agent_name)}</td>${surface}<td>${esc(repoCell(row,currentGroup()))}</td><td class="issue ${issueKlass(row)}">${issue}</td><td class="pr ${prKlass(row)}">${link(row.pr_url,row.pr_text)}</td><td class="status">${statusCell(row)}</td></tr>`}
function detailHTML(row){const columns=currentGroup()==='surface'?5:6;const parts=[];if(row.model)parts.push(esc(row.model));const title=row.github&&row.github.title;if(title)parts.push(esc(title));if((row.issues||[]).length)parts.push(issueLinks(row));return `<tr class="detail" ${state.detail?'':'hidden'}><td colspan="${columns}">${parts.join(' · ')||'no further detail'}</td></tr>`}
function render(){const message=state.message;if(!message)return;document.querySelector('#summary').innerHTML=`<span class="chip">${esc(boardSummary(message))}</span><span class="chip">grouped by ${esc(currentGroup())}</span>`;const conflicts=message.conflicts||[];const strip=document.querySelector('#conflicts');strip.innerHTML=conflicts.map(text=>`<div class="conflict">⚠ ${esc(text)}</div>`).join('');strip.hidden=!conflicts.length;document.querySelector('#surface-head').hidden=currentGroup()==='surface';document.querySelector('#repo-head').textContent=currentGroup()==='repo'?'BRANCH':'REPO / BRANCH';const columns=currentGroup()==='surface'?5:6;const sections=boardSections(message.rows||[],currentGroup());document.querySelector('#rows').innerHTML=sections.map(section=>(currentGroup()==='none'?'':`<tr class="group"><th colspan="${columns}">${esc(section.label)}</th></tr>`)+section.rows.map(row=>rowHTML(row)+detailHTML(row)).join('')).join('');document.querySelector('#empty').hidden=(message.rows||[]).length>0;document.querySelector('#board').hidden=!(message.rows||[]).length;document.querySelectorAll('[data-group]').forEach(b=>b.classList.toggle('active',b.dataset.group===currentGroup()));document.querySelector('#detail').textContent=`d ${state.detail?'hide':'show'} detail`}
function refreshAges(){const message=state.message;if(!message)return;const now=Date.now();for(const row of message.rows||[]){const node=document.querySelector(`[data-age="${row.id}"]`);if(node)node.textContent=formatAge(liveAge(row,message.epoch_ms,now))}}
function currentGroup(){return state.group!==null?state.group:boardGroup(query,state.message?.group)}
function apply(message){state.group=resolveGroup(state.group,query,message);if(!state.detailChosen)state.detail=message.detail!=='hidden';state.message=message;render()}
function setGroup(group){state.group=group;render()}
function toggleDetail(){state.detail=!state.detail;state.detailChosen=true;render()}
const es=new EventSource(base+'/board/events');es.addEventListener('board',e=>{document.querySelector('#connection').textContent='live';apply(JSON.parse(e.data))});es.onerror=()=>document.querySelector('#connection').textContent='reconnecting…';
setInterval(refreshAges,1000);
document.querySelectorAll('[data-group]').forEach(b=>b.onclick=()=>setGroup(b.dataset.group));document.querySelector('#detail').onclick=toggleDetail;
window.addEventListener('keydown',e=>{if(e.ctrlKey||e.metaKey||e.altKey)return;if(e.key==='g')setGroup(nextBoardGroup(currentGroup()||'none'));else if(e.key==='d')toggleDetail()});
</script></body></html>"""


# How long after the last board page or JSON request the roster keeps being
# refreshed with nobody streaming it. A panel whose person never opens
# /board costs nothing for it.
BOARD_INTEREST_SECONDS = 60.0


def board_wire(
    message: BoardMessage,
    *,
    settings: dict[str, str],
    discovering: bool = False,
) -> dict[str, Any]:
    """One board event: the validated message under the panel's envelope.

    This is the boundary where the typed :class:`BoardMessage` becomes JSON.
    ``group`` and ``detail`` are the ``[board]`` defaults the page starts
    with; a ``?group=`` query on the page wins over the first, and the person
    can change both once it is open.
    """
    if not isinstance(message, BoardMessage):
        raise TypeError("board_wire needs a BoardMessage")
    return {
        "schema": PANEL_SCHEMA,
        "type": BOARD_EVENT,
        "generated_at": datetime.now().astimezone().isoformat(),
        "epoch_ms": int(time.time() * 1000),
        "group": settings.get("group", BOARD_DEFAULTS["group"]),
        "detail": settings.get("detail", BOARD_DEFAULTS["detail"]),
        "discovering": discovering,
        **message.to_wire(),
    }


def empty_board_wire(settings: dict[str, str] | None = None) -> dict[str, Any]:
    """The roster before the first walk of the machine: no rows, discovering.

    It carries the configured ``[board]`` defaults, so a page that opens
    before the first real message starts grouped the way the file says.
    """
    return board_wire(
        board_rows_payload([], []),
        settings=dict(settings or BOARD_DEFAULTS),
        discovering=True,
    )


class BoardFeed:
    """The roster behind ``/board``, refreshed the way ``side-dog board`` is.

    The same discovery, identity, Git, history-tail, and GitHub readback code
    the terminal board runs, on the panel's feed thread, with the readbacks
    on an executor so a slow ``gh`` never holds the timeline back. What
    reaches the browser is :func:`board_rows_payload`, which carries no
    path; the folder states stay on this side of the boundary.
    """

    def __init__(self, *, github_poll: float = DEFAULT_GITHUB_POLL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._github_poll = github_poll
        self._states: dict[Path, BoardRootState] = {}
        self._pending: dict[Path, BoardGithubRequest] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="side-dog-board"
        )
        self._last_discovery = -1e9
        # Read now, not at the first discovery, so the placeholder the server
        # hands a page that opens early already carries the file's defaults.
        self._settings = config_board(load_config())
        self._fingerprint: str | None = None

    def _discover(self, now: float) -> None:
        if now - self._last_discovery < BOARD_DISCOVERY_SECONDS:
            return
        self._last_discovery = now
        configuration = load_config()
        # Re-read with every discovery, like the pins, so an edit to the
        # file shows up in the page's defaults without a restart.
        self._settings = config_board(configuration)
        found = discovered_watch_roots(configuration, uncapped=True)
        for root in found:
            self._states.setdefault(root, BoardRootState(root=root))
        for root in list(self._states):
            if root not in found:
                del self._states[root]
                self._pending.pop(root, None)

    def refresh(self) -> BoardMessage:
        """Bring every folder up to date and return the validated roster."""
        with self._lock:
            now = time.monotonic()
            self._discover(now)
            for state in list(self._states.values()):
                refresh_board_root(
                    state,
                    now,
                    github_poll=self._github_poll,
                    executor=self._executor,
                    pending=self._pending,
                )
            collect_board_github(self._states, self._pending)
            now_ms = int(time.time() * 1000)
            rows = sort_board_rows(
                rows_from_sources(
                    (board_source(state) for state in self._states.values()), now_ms
                )
            )
            # The browser's strip is built from no path; the terminal's
            # names the shared folder and stays in the terminal.
            return board_rows_payload(rows, browser_conflicts(rows))

    def settings(self) -> dict[str, str]:
        with self._lock:
            return dict(self._settings)

    def poll(self) -> dict[str, Any] | None:
        """The board event when something in it changed, otherwise nothing.

        Ages advance every second and are left out of the comparison: the
        page moves them forward itself from the event's ``epoch_ms``, so a
        quiet machine costs one event rather than one per poll.
        """
        message = self.refresh()
        wire = board_wire(message, settings=self.settings())
        material = {
            key: value
            for key, value in wire.items()
            if key not in {"generated_at", "epoch_ms"}
        }
        material["rows"] = [
            {key: value for key, value in row.items() if key != "age_seconds"}
            for row in wire["rows"]
        ]
        fingerprint = _json_fingerprint(material)
        if fingerprint == self._fingerprint:
            return None
        self._fingerprint = fingerprint
        return wire

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
        board_feed: BoardFeed | None = None,
    ) -> None:
        self.token = token
        self.feed = feed
        self.board_feed = board_feed
        self.poll_seconds = poll_seconds
        self._state_lock = threading.Lock()
        self._subscribers: set[queue.Queue[tuple[str, dict[str, Any]]]] = set()
        self._stop_feed = threading.Event()
        self._feed_thread: threading.Thread | None = None
        self._board_thread: threading.Thread | None = None
        # The first board event says "discovering" until the board thread
        # has walked the machine once; startup must not wait on Git or gh.
        # It carries the configured grouping so the page does not start flat.
        self._board_snapshot = empty_board_wire(
            board_feed.settings() if board_feed is not None else None
        )
        self._board_streams = 0
        self._board_interest = float("-inf")
        self.board_failures = 0
        super().__init__(address, PanelHandler)
        self._snapshot = self.feed.snapshot()
        self._feed_thread = threading.Thread(
            target=self._run_feed,
            name="side-dog-panel-feed",
            daemon=True,
        )
        self._feed_thread.start()
        if self.board_feed is not None:
            # Its own thread: discovery and a slow `git status` for the
            # roster must never hold a timeline update back.
            self._board_thread = threading.Thread(
                target=self._run_board,
                name="side-dog-panel-board",
                daemon=True,
            )
            self._board_thread.start()

    def subscribe(
        self,
    ) -> tuple[dict[str, Any], queue.Queue[tuple[str, dict[str, Any]]]]:
        updates: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        with self._state_lock:
            self._subscribers.add(updates)
            snapshot = self._snapshot
        return snapshot, updates

    def subscribe_board(
        self,
    ) -> tuple[dict[str, Any], queue.Queue[tuple[str, dict[str, Any]]]]:
        """The current board message and a queue every later message reaches.

        The queue is the same kind the timeline uses and receives every
        event; the board stream forwards only :data:`BOARD_EVENT` from it.
        """
        updates: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        with self._state_lock:
            self._subscribers.add(updates)
            self._board_streams += 1
            self._board_interest = time.monotonic()
            snapshot = self._board_snapshot
        return snapshot, updates

    def unsubscribe_board(
        self, updates: queue.Queue[tuple[str, dict[str, Any]]]
    ) -> None:
        with self._state_lock:
            self._subscribers.discard(updates)
            self._board_streams = max(0, self._board_streams - 1)
            self._board_interest = time.monotonic()

    def board_snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            self._board_interest = time.monotonic()
            return self._board_snapshot

    def board_wanted(self, now: float | None = None) -> bool:
        """Whether anyone is looking at the roster, or was a moment ago."""
        with self._state_lock:
            if self._board_streams > 0:
                return True
            moment = time.monotonic() if now is None else now
            return moment - self._board_interest < BOARD_INTEREST_SECONDS

    def unsubscribe(self, updates: queue.Queue[tuple[str, dict[str, Any]]]) -> None:
        with self._state_lock:
            self._subscribers.discard(updates)

    def publish(self, event: str, value: dict[str, Any]) -> None:
        with self._state_lock:
            if event == BOARD_EVENT:
                self._board_snapshot = value
            elif event == "snapshot":
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
            elif event == "display":
                self._snapshot = {**self._snapshot, "display": value}
            for subscriber in self._subscribers:
                subscriber.put_nowait((event, value))

    def set_show_filesystem_activity(self, show: bool) -> None:
        value = self.feed.set_show_filesystem_activity(show)
        save_filesystem_activity_setting(value)
        self.publish("display", {"show_filesystem_activity": value})

    def _run_feed(self) -> None:
        while not self._stop_feed.wait(self.poll_seconds):
            for event, value in self.feed.poll():
                self.publish(event, value)

    def _run_board(self) -> None:
        while not self._stop_feed.wait(self.poll_seconds):
            if self.board_wanted():
                self._poll_board()

    def _poll_board(self) -> None:
        if self.board_feed is None:
            return
        try:
            message = self.board_feed.poll()
        except Exception as error:
            # A folder that vanished mid-poll or a collector that raised
            # must not stop the roster for good; the next poll starts over
            # from the discovery step. Say so once per streak rather than
            # silently, so a page stuck on its last message has a reason.
            self.board_failures += 1
            if self.board_failures == 1:
                print(
                    f"side-dog: board refresh failed ({type(error).__name__});"
                    " keeping the last roster and retrying",
                    file=sys.stderr,
                )
            return
        self.board_failures = 0
        if message is not None:
            self.publish(BOARD_EVENT, message)

    def server_close(self) -> None:
        self._stop_feed.set()
        if self._feed_thread is not None:
            self._feed_thread.join(timeout=max(1.0, self.poll_seconds * 2))
        if self._board_thread is not None:
            self._board_thread.join(timeout=max(1.0, self.poll_seconds * 2))
        self.feed.close()
        if self.board_feed is not None:
            self.board_feed.close()
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

    def do_POST(self) -> None:
        if not self._allowed():
            body = b"not found\n"
            self._headers(404, "text/plain; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        path = urlsplit(self.path).path.rstrip("/")
        base = f"/{self.server.token}"
        if path != f"{base}/display":
            body = b"not found\n"
            self._headers(404, "text/plain; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > 4096:
            body = b"invalid request\n"
            self._headers(400, "text/plain; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            payload = None
        show = (
            payload.get("show_filesystem_activity")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(show, bool):
            body = b"invalid request\n"
            self._headers(400, "text/plain; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        self.server.set_show_filesystem_activity(show)
        body = json.dumps(
            {"show_filesystem_activity": show}, separators=(",", ":")
        ).encode()
        self._headers(200, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

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
        if path == f"{base}/board":
            body = BOARD_HTML.encode()
            self._headers(200, "text/html; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if path == f"{base}/board/data":
            body = json.dumps(
                self.server.board_snapshot(),
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
            self._headers(200, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if path == f"{base}/board/events":
            self._board_events()
            return
        body = b"not found\n"
        self._headers(404, "text/plain; charset=utf-8", len(body))
        self.wfile.write(body)

    def _board_events(self) -> None:
        """Stream the board: the current message first, then each change."""
        self.close_connection = True
        self._headers(200, "text/event-stream; charset=utf-8")
        snapshot, updates = self.server.subscribe_board()
        try:
            self.wfile.write(encode_sse(BOARD_EVENT, snapshot))
            self.wfile.flush()
            while True:
                try:
                    event, value = updates.get(timeout=HEARTBEAT_SECONDS)
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
                    continue
                if event != BOARD_EVENT:
                    continue
                self.wfile.write(encode_sse(event, value))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            self.server.unsubscribe_board(updates)

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
    roots: Iterable[Path],
    *,
    port: int = 0,
    poll_seconds: float = 0.75,
    follow_herdr: bool = False,
    workspace_id: str | None = None,
    requested_roots: Iterable[Path] | None = None,
    discovery_mode: DiscoveryMode | None = None,
    notify: bool = True,
    board: bool = False,
) -> tuple[PanelServer, str]:
    """The server and its private URL.

    ``board`` opts the machine-wide roster in. It is off unless the caller
    asks, so a panel built for a few named folders - the demo's synthetic
    ones above all - never shows sessions from anywhere else.
    """
    token = secrets.token_urlsafe(24)
    server = PanelServer(
        ("127.0.0.1", port),
        token,
        PanelFeed(
            roots,
            follow_herdr=follow_herdr,
            workspace_id=workspace_id,
            requested_roots=requested_roots,
            discovery_mode=discovery_mode,
            notify=notify,
        ),
        max(0.05, poll_seconds),
        board_feed=BoardFeed() if board else None,
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
    follow_herdr: bool = False,
    require_herdr: bool = False,
    workspace_id: str | None = None,
    discovery_mode_key: str | None = None,
    no_notify: bool = False,
    board: bool = True,
) -> int:
    notify = not no_notify and config_notify_enabled(load_config())
    projects = (
        [projects]
        if isinstance(projects, (str, os.PathLike))
        else list(projects)
    )
    discovery_mode = (
        discovery_mode_from_key(discovery_mode_key)
        if discovery_mode_key is not None
        else folder_discovery_mode(
            explicit_roots=bool(projects),
            follow_herdr=follow_herdr,
            require_herdr=require_herdr,
            workspace_only=workspace_id is not None,
            automatic=False,
        )
    )
    roots, requested, herdr_error = initial_watch_roots(
        projects,
        follow_herdr=follow_herdr,
        require_herdr=require_herdr,
        workspace_id=workspace_id,
    )
    if discovery_mode.key == "automatic":
        # The terminal passes its current discovered roots as the panel's
        # initial picture. They are borrowed seats, not named folders, so the
        # panel must remain free to replace them as machine-wide activity moves.
        requested = set()
    if follow_herdr and herdr_error:
        print(
            f"side-dog: {herdr_error}; watching available folders and retrying",
            file=sys.stderr,
        )
    server, url = create_panel_server(
        roots,
        port=port,
        poll_seconds=poll_seconds,
        follow_herdr=follow_herdr,
        workspace_id=workspace_id,
        requested_roots=requested,
        discovery_mode=discovery_mode,
        notify=notify,
        board=board,
    )
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
