"""
Healer control plane: wires Watchdog → Diagnostician → Reproducer → Fixer →
(optional human gate) → Deployer, and serves a real-time SSE dashboard.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

from config import (
    APP_SRC,
    APPROVAL_MODE,
    APPROVAL_TIMEOUT_SEC,
    CANARY_CONTAINER,
    CANARY_URL,
    HEALTH_URL,
    TARGET_CONTAINER,
)
from deployer import Deployer
from diagnostician import Diagnostician
from events import EventBus
from fixer import Fixer
from reproducer import Reproducer
from source_index import SourceIndex
from watchdog import CrashEvent, Watchdog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("healer")

bus = EventBus()
_pipeline_lock = asyncio.Lock()
_watchdog_task: asyncio.Task | None = None
_approval_future: asyncio.Future | None = None


async def on_crash(event: CrashEvent):
    if _pipeline_lock.locked():
        logger.warning("Crash event ignored — a healing pipeline is already running")
        return
    async with _pipeline_lock:
        try:
            await heal(event)
        except Exception:
            logger.exception("Healing pipeline crashed")
            await bus.emit("healed", "Pipeline aborted with an internal error", ok=False)


async def heal(event: CrashEvent):
    await bus.emit("watchdog", f"Crash detected in {event.container_name}",
                   traceback=event.traceback[-2000:])

    # 1. Diagnose
    await bus.emit("diagnostician", "Analyzing traceback against indexed source…")
    index = SourceIndex(APP_SRC)
    diagnosis = await Diagnostician().diagnose(event, index)
    await bus.emit("diagnostician",
                   f"Root cause ({diagnosis.confidence:.0%} confidence): {diagnosis.root_cause}",
                   file=diagnosis.affected_file, buggy_code=diagnosis.buggy_code)

    # 2. Reproduce — the failing test comes before any patch
    await bus.emit("reproducer", "Writing a failing test in an ephemeral container…")
    repro = await Reproducer().reproduce(diagnosis, index)
    if not repro or not repro.verified_failing:
        await bus.emit("healed", "Could not reproduce the bug with a failing test — "
                                 "refusing to patch blind", ok=False)
        return
    await bus.emit("reproducer", f"Bug reproduced: {repro.test_path} fails on current code",
                   test_code=repro.test_code)

    # 3. Fix — iterate until repro passes and full suite stays green
    await bus.emit("fixer", "Patching until the failing test passes and the suite stays green…")
    fix = await Fixer().fix(diagnosis, repro, index)
    if not fix.success:
        await bus.emit("healed", f"Fixer gave up after {fix.attempts} attempts — "
                                 "changes rolled back", ok=False)
        return
    await bus.emit("fixer", f"Verified fix on attempt {fix.attempts}: repro test passes, "
                            "full suite green", diff=fix.diff, explanation=fix.explanation)

    # 4. Optional human gate
    if APPROVAL_MODE == "dashboard":
        await bus.emit("gate", "Verified patch awaiting one-click approval below",
                       pending=True, diff=fix.diff)
        approved = await _wait_dashboard_approval()
        await bus.emit("gate", "Approved — deploying" if approved else "Rejected")
    else:
        approved = True
        await bus.emit("gate", "Human gate off — fully autonomous mode")
    if not approved:
        fix.rollback()
        await bus.emit("healed", "Patch rejected at human gate — changes rolled back", ok=False)
        return

    # 5. Canary deploy
    promoted = await Deployer(bus).deploy()
    if not promoted:
        fix.rollback()
        await bus.emit("healed", "Canary failed in production — rolled back to stable", ok=False)
        return

    await bus.emit("healed", "Bug detected, diagnosed, reproduced, patched, and shipped "
                             "to 100% of traffic — zero human intervention", ok=True)
    _rearm_watchdog(CANARY_CONTAINER, f"{CANARY_URL}/health")


async def _wait_dashboard_approval() -> bool:
    """Block the pipeline on a one-click decision from the dashboard.
    Timeout rejects (fail safe)."""
    global _approval_future
    _approval_future = asyncio.get_running_loop().create_future()
    try:
        return await asyncio.wait_for(_approval_future, timeout=APPROVAL_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        logger.warning("Dashboard approval timed out — rejecting")
        return False
    finally:
        _approval_future = None


def _rearm_watchdog(container: str, health_url: str):
    """After promotion the canary is the new primary — watch it."""
    global _watchdog_task
    if _watchdog_task:
        _watchdog_task.cancel()
    _watchdog_task = asyncio.create_task(_run_watchdog(container, health_url))


async def _run_watchdog(container: str, health_url: str):
    while True:
        try:
            await Watchdog(container, health_url, on_crash).run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Watchdog died ({exc}); restarting in 5s")
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _watchdog_task
    _watchdog_task = asyncio.create_task(_run_watchdog(TARGET_CONTAINER, HEALTH_URL))
    await bus.emit("watchdog", f"Watching {TARGET_CONTAINER} ({HEALTH_URL})")
    yield
    if _watchdog_task:
        _watchdog_task.cancel()


app = FastAPI(title="Self-Healer Control Plane", lifespan=lifespan)


@app.get("/events")
async def events():
    q = bus.subscribe()

    async def gen():
        try:
            for e in bus.history:
                yield bus.sse(e)
            while True:
                yield bus.sse(await q.get())
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.post("/gate/{decision}")
async def gate_decision(decision: str):
    if _approval_future and not _approval_future.done() and decision in ("approve", "reject"):
        _approval_future.set_result(decision == "approve")
        return {"ok": True}
    return {"ok": False, "reason": "nothing pending"}


@app.post("/demo/trigger")
async def demo_trigger():
    """Fire the seeded bug on the target app so a healing run can be demoed
    from the dashboard without touching a terminal."""
    import httpx
    base = HEALTH_URL.rsplit("/health", 1)[0]
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.post(base + "/trigger-bug")
            return {"ok": True, "status": r.status_code}
    except httpx.HTTPError as exc:
        # a 5xx surfaces as a normal response above; this is connect-level failure
        return {"ok": False, "error": str(exc)}


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cicatrixa — Control Plane</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'%3E%3Crect width='512' height='512' rx='104' fill='%230A0F0C'/%3E%3Cpath d='M150 150 L256 256' stroke='%23f85149' stroke-width='36' stroke-linecap='round'/%3E%3Cpath d='M256 256 L362 362' stroke='%233fb950' stroke-width='36' stroke-linecap='round'/%3E%3Cg stroke='%23c9d1d9' stroke-width='17' stroke-linecap='round'%3E%3Cpath d='M168 216 L216 168'/%3E%3Cpath d='M211 259 L259 211'/%3E%3Cpath d='M253 301 L301 253'/%3E%3Cpath d='M296 344 L344 296'/%3E%3C/g%3E%3C/svg%3E">
<style>
  :root{--bg:#0A0F0C;--panel:#101713;--card:#0C1210;--ink:#E4EEE7;--muted:#74857B;
        --line:#1F2B24;--red:#FF5257;--green:#40D967;--green-dim:#2C7A45;--amber:#D9A440}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  a{color:var(--green)}

  header{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:14px;
         padding:14px 22px;background:rgba(10,15,12,.9);backdrop-filter:blur(8px);
         border-bottom:1px solid var(--line)}
  .mark{width:26px;height:26px;flex-shrink:0}
  .brand{font-size:16px;font-weight:600;letter-spacing:-.02em}
  .brand .r{color:var(--red)}.brand .g{color:var(--green)}
  .brand .sub{color:var(--muted);font-weight:400;font-size:13px}
  .chips{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}
  .chip{font-size:11.5px;color:var(--muted);border:1px solid var(--line);
        border-radius:99px;padding:4px 12px;letter-spacing:.04em}
  .chip b{color:var(--ink);font-weight:500}
  .chip.live{color:var(--green);border-color:var(--green-dim)}
  .chip.live::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;
        background:var(--green);margin-right:7px;animation:blink 1.4s steps(1) infinite}
  .chip.dead{color:var(--red);border-color:var(--red)}
  @keyframes blink{50%{opacity:.25}}

  main{max-width:1080px;margin:0 auto;padding:26px 22px 60px}

  .track{display:flex;align-items:center;gap:0;margin-bottom:22px;overflow-x:auto;padding-bottom:4px}
  .st{display:flex;align-items:center;flex-shrink:0}
  .st .pill{font-size:12px;letter-spacing:.06em;color:#43514a;border:1px solid var(--line);
            border-radius:99px;padding:6px 16px;background:var(--panel);transition:all .3s;white-space:nowrap}
  .st.active .pill{color:#06130A;background:var(--amber);border-color:var(--amber);animation:pulse 1.2s infinite}
  .st.done .pill{color:var(--green);border-color:var(--green-dim)}
  .st.failed .pill{color:var(--red);border-color:var(--red)}
  .st .link{width:26px;height:1px;background:var(--line);flex-shrink:0}
  .st.done .link{background:var(--green-dim)}
  @keyframes pulse{50%{opacity:.55}}

  .toolbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:18px}
  .stat{font-size:12.5px;color:var(--muted)}
  .stat b{color:var(--ink);font-weight:600}
  .spacer{flex:1}
  button{font:inherit;cursor:pointer;border-radius:8px;padding:8px 18px;transition:all .2s}
  .fire{background:var(--red);color:#140607;border:none;font-weight:600}
  .fire:hover{box-shadow:0 6px 24px rgba(255,82,87,.35);transform:translateY(-1px)}
  .fire:disabled{opacity:.5;cursor:default;transform:none;box-shadow:none}
  .ghost{background:transparent;color:var(--muted);border:1px solid var(--line)}
  .ghost.on{color:var(--green);border-color:var(--green-dim)}

  #log{display:grid;gap:10px}
  .evt{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--line);
       border-radius:10px;padding:13px 18px;animation:enter .35s ease}
  @keyframes enter{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
  .evt .head{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
  .evt .tag{font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;
            border-radius:5px;padding:2px 9px;background:var(--panel);color:var(--muted)}
  .evt .msg{color:var(--ink)}
  .evt .tm{margin-left:auto;font-size:11.5px;color:#43514a}
  .evt.s-watchdog{border-left-color:var(--red)}      .evt.s-watchdog .tag{color:var(--red)}
  .evt.s-diagnostician{border-left-color:#5b8bd6}    .evt.s-diagnostician .tag{color:#5b8bd6}
  .evt.s-reproducer{border-left-color:var(--amber)}  .evt.s-reproducer .tag{color:var(--amber)}
  .evt.s-fixer{border-left-color:#b57edc}            .evt.s-fixer .tag{color:#b57edc}
  .evt.s-gate{border-left-color:var(--ink)}          .evt.s-gate .tag{color:var(--ink)}
  .evt.s-deployer{border-left-color:#4fd1c5}         .evt.s-deployer .tag{color:#4fd1c5}
  .evt.s-healed.ok{border-left-color:var(--green);background:rgba(64,217,103,.05)}
  .evt.s-healed.ok .tag{color:var(--green)}
  .evt.s-healed.bad{border-left-color:var(--red);background:rgba(255,82,87,.05)}
  .evt.s-healed.bad .tag{color:var(--red)}

  details{margin-top:10px;border:1px solid var(--line);border-radius:8px;overflow:hidden}
  summary{cursor:pointer;padding:8px 14px;font-size:12px;color:var(--muted);background:var(--panel);
          list-style:none;letter-spacing:.06em}
  summary::-webkit-details-marker{display:none}
  summary::before{content:"▸ ";color:var(--green)}
  details[open] summary::before{content:"▾ "}
  pre{margin:0;padding:12px 14px;font-size:12px;line-height:1.7;overflow-x:auto;color:#a5c9b2}
  pre .add{color:#7ee29b;background:rgba(64,217,103,.07);display:block}
  pre .del{color:#ff8e91;background:rgba(255,82,87,.07);display:block}
  pre .hunk{color:#5b8bd6;display:block}

  .gatebtns{margin-top:12px;display:flex;gap:10px}
  .gatebtns .ok{background:var(--green);color:#06130A;border:none;font-weight:600}
  .gatebtns .no{background:transparent;border:1px solid var(--red);color:var(--red)}

  .empty{text-align:center;color:var(--muted);padding:70px 20px;border:1px dashed var(--line);border-radius:12px}
  .empty .big{font-size:15px;color:var(--ink);margin-bottom:6px}
</style></head><body>

<header>
  <svg class="mark" viewBox="0 0 512 512" aria-hidden="true">
    <rect width="512" height="512" rx="104" fill="#0A0F0C" stroke="#1F2B24"/>
    <path d="M150 150 L256 256" stroke="#FF5257" stroke-width="36" stroke-linecap="round"/>
    <path d="M256 256 L362 362" stroke="#40D967" stroke-width="36" stroke-linecap="round"/>
    <g stroke="#c9d1d9" stroke-width="17" stroke-linecap="round">
      <path d="M168 216 L216 168"/><path d="M211 259 L259 211"/>
      <path d="M253 301 L301 253"/><path d="M296 344 L344 296"/>
    </g>
  </svg>
  <span class="brand"><span class="r">cica</span><span class="g">trixa</span>
    <span class="sub">/ control plane</span></span>
  <div class="chips">
    <span class="chip">watching <b>__TARGET__</b></span>
    <span class="chip">model <b>__MODEL__</b></span>
    <span class="chip">gate <b>__MODE__</b></span>
    <span class="chip live" id="conn">connected</span>
  </div>
</header>

<main>
  <div class="track" id="stages"></div>
  <div class="toolbar">
    <span class="stat">incidents this session: <b id="ninc">0</b></span>
    <span class="stat" id="timer"></span>
    <span class="spacer"></span>
    <button class="ghost on" id="autoscroll">autoscroll</button>
    <button class="fire" id="fire">⚡ trigger demo bug</button>
  </div>
  <div id="log">
    <div class="empty" id="empty">
      <div class="big">All quiet — watching production.</div>
      Fire the demo bug to watch a full healing run, or wait for a real crash.
    </div>
  </div>
</main>

<script>
const STAGES=["watchdog","diagnostician","reproducer","fixer","gate","deployer","healed"];
const stagesEl=document.getElementById("stages"),logEl=document.getElementById("log");
const emptyEl=document.getElementById("empty"),connEl=document.getElementById("conn");
const nincEl=document.getElementById("ninc"),timerEl=document.getElementById("timer");
let ninc=0,healStart=null,timerIv=null,autoscroll=true;

STAGES.forEach((s,j)=>{
  const w=document.createElement("div");w.className="st";w.id="st-"+s;
  w.innerHTML=(j?'<span class="link"></span>':'')+'<span class="pill">'+s+'</span>';
  stagesEl.appendChild(w);
});
const idx=s=>STAGES.indexOf(s);

document.getElementById("autoscroll").onclick=function(){autoscroll=!autoscroll;this.classList.toggle("on",autoscroll)};
document.getElementById("fire").onclick=async function(){
  this.disabled=true;this.textContent="firing…";
  try{await fetch("/demo/trigger",{method:"POST"})}catch(e){}
  const b=this;setTimeout(()=>{b.disabled=false;b.textContent="⚡ trigger demo bug"},6000);
};

function fmtDiff(t){
  return esc(t).split("\\n").map(l=>{
    if(l.startsWith("+"))return '<span class="add">'+l+"</span>";
    if(l.startsWith("-"))return '<span class="del">'+l+"</span>";
    if(l.startsWith("@@"))return '<span class="hunk">'+l+"</span>";
    return l;
  }).join("\\n");
}
function block(title,body,isDiff,open){
  return '<details'+(open?' open':'')+'><summary>'+title+'</summary><pre>'+
         (isDiff?fmtDiff(body):esc(body))+"</pre></details>";
}
function tick(){
  if(healStart===null)return;
  const s=((Date.now()-healStart)/1000)|0;
  timerEl.innerHTML="elapsed: <b>"+((s/60)|0)+"m "+(s%60)+"s</b>";
}

const es=new EventSource("/events");
es.onopen=()=>{connEl.textContent="connected";connEl.className="chip live"};
es.onerror=()=>{connEl.textContent="reconnecting…";connEl.className="chip dead"};
es.onmessage=m=>{
  const e=JSON.parse(m.data),i=idx(e.stage);
  if(emptyEl.parentNode)emptyEl.remove();

  if(e.stage==="watchdog"&&e.data.traceback){
    ninc++;nincEl.textContent=ninc;
    healStart=Date.now();clearInterval(timerIv);timerIv=setInterval(tick,1000);tick();
  }
  if(e.stage==="healed"){clearInterval(timerIv);tick();}

  STAGES.forEach((s,j)=>{const el=document.getElementById("st-"+s);
    el.className="st"+(j<i?" done":j===i?(e.stage==="healed"?(e.data.ok?" done":" failed"):" active"):"");
  });

  const div=document.createElement("div");
  div.className="evt s-"+e.stage+(e.stage==="healed"?(e.data.ok?" ok":" bad"):"");
  let html='<div class="head"><span class="tag">'+e.stage+'</span>'+
           '<span class="msg">'+esc(e.message)+'</span>'+
           '<span class="tm">'+new Date().toLocaleTimeString()+"</span></div>";
  if(e.data.traceback)html+=block("traceback",e.data.traceback,false,false);
  if(e.data.buggy_code)html+=block("suspect code — "+esc(e.data.file||""),e.data.buggy_code,false,false);
  if(e.data.test_code)html+=block("reproduction test (the proof)",e.data.test_code,false,true);
  if(e.data.diff)html+=block("verified patch",e.data.diff,true,true);
  if(e.data.pending)html+='<div class="gatebtns">'+
    '<button class="ok" onclick="gate(\\'approve\\',this)">✓ approve &amp; deploy</button>'+
    '<button class="no" onclick="gate(\\'reject\\',this)">✗ reject &amp; roll back</button></div>';
  div.innerHTML=html;logEl.appendChild(div);
  if(autoscroll)div.scrollIntoView({behavior:"smooth",block:"end"});
};
function gate(d,btn){fetch("/gate/"+d,{method:"POST"});btn.parentElement.remove()}
function esc(s){return String(s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}
</script></body></html>"""

DASHBOARD_HTML = (DASHBOARD_HTML
                  .replace("__TARGET__", TARGET_CONTAINER)
                  .replace("__MODEL__", os.getenv("HEALER_MODEL", "gpt-5.1"))
                  .replace("__MODE__", APPROVAL_MODE))
