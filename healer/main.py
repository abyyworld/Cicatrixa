"""
Healer control plane: wires Watchdog → Diagnostician → Reproducer → Fixer →
(optional human gate) → Deployer, and serves a real-time SSE dashboard.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

import telegram_gate
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
    elif APPROVAL_MODE == "telegram":
        await bus.emit("gate", "Awaiting one-tap approval on Telegram…")
        approved = await telegram_gate.request_approval(fix.diff, fix.explanation)
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


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Self-Healer</title>
<style>
  body{background:#0b0e14;color:#c9d1d9;font:14px/1.5 -apple-system,Menlo,monospace;margin:0;padding:24px}
  h1{font-size:18px;color:#e6edf3;margin:0 0 4px}
  .sub{color:#8b949e;margin-bottom:20px}
  .stages{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:24px}
  .stage{padding:8px 14px;border-radius:8px;border:1px solid #30363d;color:#8b949e;background:#161b22}
  .stage.active{border-color:#d29922;color:#d29922;animation:pulse 1.2s infinite}
  .stage.done{border-color:#3fb950;color:#3fb950}
  .stage.failed{border-color:#f85149;color:#f85149}
  @keyframes pulse{50%{opacity:.4}}
  #log{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:16px;max-height:60vh;overflow:auto}
  .evt{margin-bottom:10px;border-left:3px solid #30363d;padding-left:10px}
  .evt .tag{color:#58a6ff;font-weight:600}
  .evt pre{background:#161b22;padding:8px;border-radius:6px;overflow-x:auto;color:#a5d6ff;font-size:12px}
  .evt pre.diff{color:#c9d1d9}
  .gatebtns button{margin:6px 8px 0 0;padding:6px 16px;border-radius:6px;background:#21262d;cursor:pointer;font:inherit}
  .gatebtns .ok{border:1px solid #3fb950;color:#3fb950}
  .gatebtns .no{border:1px solid #f85149;color:#f85149}
</style></head><body>
<h1>🩹 Self-Healing Production System</h1>
<div class="sub">watchdog → diagnostician → reproducer → fixer → gate → deployer</div>
<div class="stages" id="stages"></div>
<div id="log"></div>
<script>
const STAGES=["watchdog","diagnostician","reproducer","fixer","gate","deployer","healed"];
const stagesEl=document.getElementById("stages"),logEl=document.getElementById("log");
STAGES.forEach(s=>{const d=document.createElement("div");d.className="stage";d.id="st-"+s;d.textContent=s;stagesEl.appendChild(d)});
const idx=s=>STAGES.indexOf(s);
new EventSource("/events").onmessage=m=>{
  const e=JSON.parse(m.data),i=idx(e.stage);
  STAGES.forEach((s,j)=>{const el=document.getElementById("st-"+s);
    if(j<i)el.className="stage done";
    else if(j===i)el.className="stage "+(e.stage==="healed"?(e.data.ok?"done":"failed"):"active");
  });
  const div=document.createElement("div");div.className="evt";
  let html=`<span class="tag">[${e.stage}]</span> ${esc(e.message)}`;
  if(e.data.diff)html+=`<pre class="diff">${esc(e.data.diff)}</pre>`;
  if(e.data.test_code)html+=`<pre>${esc(e.data.test_code)}</pre>`;
  if(e.data.traceback)html+=`<pre>${esc(e.data.traceback)}</pre>`;
  if(e.data.pending)html+=`<div class="gatebtns"><button class="ok" onclick="gate('approve',this)">✅ Approve &amp; deploy</button><button class="no" onclick="gate('reject',this)">❌ Reject &amp; roll back</button></div>`;
  div.innerHTML=html;logEl.appendChild(div);logEl.scrollTop=logEl.scrollHeight;
};
function gate(d,btn){fetch("/gate/"+d,{method:"POST"});btn.parentElement.remove()}
function esc(s){return String(s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}
</script></body></html>"""
