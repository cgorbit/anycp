"""anycp meta-server — orchestrates file transfers between hosts.

Handles ONLY metadata and coordination.  No file data passes through.
Both sides communicate via long-polling; the TransferCoordinator drives
protocol selection, issues commands, handles retries.
"""

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Depends
from pydantic import BaseModel

# --- Config ---
TOKEN = os.environ.get("ANYCP_TOKEN", "")
TTL = int(os.environ.get("ANYCP_TTL", "3600"))
POLL_TIMEOUT = int(os.environ.get("ANYCP_POLL_TIMEOUT", "30"))

# --- Storage ---
transfers: dict[str, dict] = {}


# --- Auth ---
async def auth(request: Request):
    if not TOKEN:
        raise HTTPException(500, "ANYCP_TOKEN not configured on server")
    if request.headers.get("Authorization") != f"Bearer {TOKEN}":
        raise HTTPException(401, "Unauthorized")


# ===================================================================
# Event system — each transfer has two event queues (push / pop)
# ===================================================================

def emit(tid: str, side: str, event_type: str, data: dict | None = None):
    """Append an event to a side's queue and wake any long-poller."""
    t = transfers.get(tid)
    if not t:
        return
    q = t[side + "_events"]
    q.append({"seq": len(q) + 1, "type": event_type, "data": data or {}})
    t["_notify_" + side].set()


# ===================================================================
# TransferCoordinator — async coroutine per transfer
# ===================================================================

PROTOCOL_PRIORITY = ["sky", "http"]


def _fail(tid: str, error: str):
    t = transfers.get(tid)
    if not t:
        return
    t["status"] = "failed"
    emit(tid, "push", "transfer_failed", {"error": error})
    emit(tid, "pop",  "transfer_failed", {"error": error})


async def wait_msg(inbox: asyncio.Queue, from_side: str, timeout: float = 300):
    """Read from inbox, returning the first message from the expected side."""
    while True:
        m = await asyncio.wait_for(inbox.get(), timeout=timeout)
        if m["side"] == from_side:
            return m


async def coordinate_http(tid, t, inbox):
    """Try HTTP protocol: push starts server, pop downloads."""
    emit(tid, "push", "start_http_server", {})
    reply = await wait_msg(inbox, from_side="push")
    if reply["type"] == "error":
        return False
    if reply["type"] != "http_server_ready":
        return False
    emit(tid, "pop", "download_http", reply["data"])
    t["status"] = "transferring"
    result = await wait_msg(inbox, from_side="pop")
    return result["type"] == "download_complete"


async def coordinate_sky(tid, t, inbox):
    """Try Sky protocol: push shares, pop downloads."""
    emit(tid, "push", "start_sky_share", {})
    reply = await wait_msg(inbox, from_side="push")
    if reply["type"] == "error":
        return False
    if reply["type"] != "sky_share_ready":
        return False
    emit(tid, "pop", "download_sky", reply["data"])
    t["status"] = "transferring"
    result = await wait_msg(inbox, from_side="pop")
    return result["type"] == "download_complete"


PROTOCOL_COORDINATORS = {
    "http": coordinate_http,
    "sky":  coordinate_sky,
}


async def coordinate(tid: str):
    """Live coroutine that drives one transfer to completion."""
    t = transfers[tid]
    inbox = t["_inbox"]

    push_caps = set(t.get("push_caps", []))
    pop_caps  = set(t.get("pop_caps", []))
    common_caps = push_caps & pop_caps

    if not common_caps:
        _fail(tid, "No common_caps protocol (push={}, pop={})".format(
            sorted(push_caps), sorted(pop_caps)))
        return

    emit(tid, "push", "claimed", {"pop_host": t["pop_host"]})

    protocols_to_try = [p for p in PROTOCOL_PRIORITY if p in common_caps]
    if not protocols_to_try:
        protocols_to_try = list(common_caps)

    for protocol in protocols_to_try:
        t["protocol"] = protocol
        coord_fn = PROTOCOL_COORDINATORS.get(protocol)
        if not coord_fn:
            continue
        try:
            if await coord_fn(tid, t, inbox):
                t["status"] = "completed"
                t["completed_at"] = time.time()
                emit(tid, "push", "transfer_complete", {})
                emit(tid, "pop",  "transfer_complete", {})
                return
        except asyncio.TimeoutError:
            _fail(tid, "Timeout during {} protocol".format(protocol))
            return

    _fail(tid, "All protocols failed")


# ===================================================================
# Background cleanup
# ===================================================================

async def cleanup_loop():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for tid in list(transfers):
            t = transfers.get(tid)
            if t and now - t["created_at"] > TTL:
                if t.get("_task"):
                    t["_task"].cancel()
                t["_notify_push"].set()
                t["_notify_pop"].set()
                transfers.pop(tid, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(cleanup_loop())
    yield
    task.cancel()


app = FastAPI(title="anycp", lifespan=lifespan)


# ===================================================================
# Models
# ===================================================================

class OfferCreate(BaseModel):
    filename: str
    size: int = 0
    checksum: str = ""
    capabilities: list[str] = []
    push_host: str = ""
    path: str = ""
    abs_path: str = ""
    resolved_path: str = ""
    cwd: str = ""


class ClaimBody(BaseModel):
    pop_host: str = ""
    capabilities: list[str] = []


class MsgBody(BaseModel):
    type: str
    data: dict = {}


# ===================================================================
# Routes
# ===================================================================

@app.get("/health")
async def health():
    return {"status": "ok", "transfers": len(transfers)}


@app.post("/api/transfers", dependencies=[Depends(auth)])
async def create_transfer(body: OfferCreate):
    """Push side registers a file offer."""
    tid = uuid.uuid4().hex[:12]
    transfers[tid] = {
        "id": tid,
        "filename": body.filename,
        "size": body.size,
        "checksum": body.checksum,
        "push_host": body.push_host,
        "push_caps": body.capabilities,
        "path": body.path,
        "abs_path": body.abs_path,
        "resolved_path": body.resolved_path,
        "cwd": body.cwd,
        "pop_host": None,
        "pop_caps": None,
        "protocol": None,
        "status": "waiting",
        "created_at": time.time(),
        "completed_at": None,
        "push_events": [],
        "pop_events": [],
        "_notify_push": asyncio.Event(),
        "_notify_pop": asyncio.Event(),
        "_inbox": asyncio.Queue(),
        "_task": None,
    }
    return {"id": tid}


@app.get("/api/transfers", dependencies=[Depends(auth)])
async def list_transfers():
    """Pop side lists available offers."""
    return [
        {k: t[k] for k in (
            "id", "filename", "size", "checksum", "push_host",
            "path", "status", "created_at"
        )}
        for t in transfers.values()
        if t["status"] == "waiting"
    ]


@app.get("/api/transfers/{tid}", dependencies=[Depends(auth)])
async def get_transfer(tid: str):
    t = transfers.get(tid)
    if not t:
        raise HTTPException(404, "Transfer not found")
    return {k: v for k, v in t.items() if not k.startswith("_")}


@app.post("/api/transfers/{tid}/claim", dependencies=[Depends(auth)])
async def claim_transfer(tid: str, body: ClaimBody):
    """Pop side claims an offer — triggers coordination."""
    t = transfers.get(tid)
    if not t:
        raise HTTPException(404, "Transfer not found")
    if t["status"] != "waiting":
        raise HTTPException(400, "Transfer not available (status: {})".format(
            t["status"]))

    t["pop_host"] = body.pop_host
    t["pop_caps"] = body.capabilities
    t["status"] = "coordinating"

    t["_task"] = asyncio.create_task(coordinate(tid))

    return {"ok": True}


@app.get("/api/transfers/{tid}/poll/{side}", dependencies=[Depends(auth)])
async def poll_events(tid: str, side: str, seq: int = 0):
    """Long-poll for events.  Holds connection up to POLL_TIMEOUT seconds."""
    if side not in ("push", "pop"):
        raise HTTPException(400, "side must be 'push' or 'pop'")
    t = transfers.get(tid)
    if not t:
        raise HTTPException(404, "Transfer not found")

    events_key = side + "_events"
    notify = t["_notify_" + side]

    # Return immediately if there are new events
    new = [e for e in t[events_key] if e["seq"] > seq]
    if new:
        return {"events": new}

    # Long-poll — wait for notification or timeout
    notify.clear()
    try:
        await asyncio.wait_for(notify.wait(), timeout=POLL_TIMEOUT)
    except asyncio.TimeoutError:
        pass

    # Re-fetch (transfer may have been deleted while waiting)
    t = transfers.get(tid)
    if not t:
        return {"events": []}
    return {"events": [e for e in t[events_key] if e["seq"] > seq]}


@app.post("/api/transfers/{tid}/msg/{side}", dependencies=[Depends(auth)])
async def send_message(tid: str, side: str, body: MsgBody):
    """Receive a message from push or pop side, feed it to coordinator."""
    if side not in ("push", "pop"):
        raise HTTPException(400, "side must be 'push' or 'pop'")
    t = transfers.get(tid)
    if not t:
        raise HTTPException(404, "Transfer not found")

    await t["_inbox"].put({"side": side, "type": body.type, "data": body.data})
    return {"ok": True}


@app.delete("/api/transfers/{tid}", dependencies=[Depends(auth)])
async def delete_transfer(tid: str):
    t = transfers.pop(tid, None)
    if not t:
        raise HTTPException(404, "Transfer not found")
    if t.get("_task"):
        t["_task"].cancel()
    t["_notify_push"].set()
    t["_notify_pop"].set()
    return {"status": "deleted"}
