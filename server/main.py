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
# TransferCoordinator
# ===================================================================

PROTOCOL_PRIORITY = ["sky", "http"]

# protocol name  →  command sent to push side
PROTO_TO_PUSH_CMD = {
    "http": "start_http_server",
    "sky":  "start_sky_share",
}

# push reply  →  command sent to pop side
PUSH_REPLY_TO_POP_CMD = {
    "http_server_ready": "download_http",
    "sky_share_ready":   "download_sky",
}


def _start_protocol(tid: str, protocol: str):
    t = transfers[tid]
    t["protocol"] = protocol
    cmd = PROTO_TO_PUSH_CMD.get(protocol)
    if not cmd:
        _fail(tid, f"Unknown protocol: {protocol}")
        return
    emit(tid, "push", cmd, {})


def _fail(tid: str, error: str):
    t = transfers.get(tid)
    if not t:
        return
    t["status"] = "failed"
    emit(tid, "push", "transfer_failed", {"error": error})
    emit(tid, "pop",  "transfer_failed", {"error": error})


def on_claimed(tid: str):
    """Called when pop claims a transfer. Start coordination."""
    t = transfers[tid]

    emit(tid, "push", "claimed", {"pop_host": t["pop_host"]})

    push_caps = set(t.get("push_caps", []))
    pop_caps  = set(t.get("pop_caps", []))
    common_caps = push_caps & pop_caps

    if not common_caps:
        _fail(tid, "No common_caps protocol (push={}, pop={})".format(
            sorted(push_caps), sorted(pop_caps)))
        return

    # Pick best protocol by priority
    for p in PROTOCOL_PRIORITY:
        if p in common_caps:
            _start_protocol(tid, p)
            return
    _start_protocol(tid, common_caps.pop())


def on_message(tid: str, side: str, msg_type: str, msg_data: dict):
    """Handle a message from push or pop side."""
    t = transfers.get(tid)
    if not t:
        return

    if side == "push":
        pop_cmd = PUSH_REPLY_TO_POP_CMD.get(msg_type)
        if pop_cmd:
            emit(tid, "pop", pop_cmd, msg_data)
            t["status"] = "transferring"
        elif msg_type == "error":
            _fail(tid, msg_data.get("error", "push-side error"))

    elif side == "pop":
        if msg_type == "download_complete":
            t["status"] = "completed"
            t["completed_at"] = time.time()
            emit(tid, "push", "transfer_complete", {})
            emit(tid, "pop",  "transfer_complete", {})
        elif msg_type == "download_failed":
            # TODO: retry with next protocol
            _fail(tid, msg_data.get("error", "download failed"))
        elif msg_type == "protocol_chosen":
            proto = msg_data.get("protocol")
            if proto:
                _start_protocol(tid, proto)
            else:
                _fail(tid, "No protocol chosen")


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
                # Wake any long-polling clients before removing
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

    on_claimed(tid)

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

    on_message(tid, side, body.type, body.data)
    return {"ok": True}


@app.delete("/api/transfers/{tid}", dependencies=[Depends(auth)])
async def delete_transfer(tid: str):
    t = transfers.pop(tid, None)
    if not t:
        raise HTTPException(404, "Transfer not found")
    t["_notify_push"].set()
    t["_notify_pop"].set()
    return {"status": "deleted"}
