"""
Board Engine server — the Mac-free path.

  meeting bot (Recall.ai) ──webhook──▶ /transcript ──▶ buffer
                                                        │ every TICK_SECONDS
                                                        ▼
                                                 processor.process_chunk()
                                                        │
                                                        ▼
                                               state.json (the board)
                                                        │
      Vision Pro Safari / Paul's browser ◀── GET / (board page) + /events (SSE)
                                                        │
      actions with status "queued" ──▶ dispatcher (Claude Code headless) ──▶ results/*.html

Run:  uvicorn server:app --host 0.0.0.0 --port 8080
Env:  ANTHROPIC_API_KEY, RECALL_WEBHOOK_SECRET (optional), BOARD_TOKEN (shared secret in the board URL)
"""
from __future__ import annotations

import asyncio, json, os, time, hmac, hashlib, subprocess, shlex
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse

import processor

ROOT = Path(__file__).parent
STATE = ROOT / "state.json"
RESULTS = ROOT / "results"; RESULTS.mkdir(exist_ok=True)
TICK = int(os.environ.get("TICK_SECONDS", "30"))
TOKEN = os.environ.get("BOARD_TOKEN", "")
SECRET = os.environ.get("RECALL_WEBHOOK_SECRET", "")

app = FastAPI()
buffer: list[str] = []
subscribers: set[asyncio.Queue] = set()
lock = asyncio.Lock()


def load() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {"ventures": [], "board": {}, "actions": []}


def save(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=1))


async def broadcast(state: dict) -> None:
    payload = json.dumps(state)
    for q in list(subscribers):
        q.put_nowait(payload)


def check_token(req: Request) -> None:
    if TOKEN and req.query_params.get("t") != TOKEN:
        raise HTTPException(403)


# ---- 1. transcript intake ---------------------------------------------------
@app.post("/transcript")
async def transcript(req: Request):
    body = await req.body()
    if SECRET:
        sig = req.headers.get("x-recall-signature", "")
        if not hmac.compare_digest(sig, hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()):
            raise HTTPException(401)
    evt = json.loads(body)
    # Recall "transcript.data" events carry words + participant; adapt here for another bot.
    data = evt.get("data", {}).get("data", evt.get("data", {}))
    speaker = (data.get("participant") or {}).get("name") or "Speaker"
    words = data.get("words") or []
    text = " ".join(w.get("text", "") for w in words) if words else data.get("text", "")
    if text:
        buffer.append(f"{speaker}: {text}")
    return {"ok": True}


# ---- 2. tick: transcript → board -------------------------------------------
async def ticker():
    while True:
        await asyncio.sleep(TICK)
        if not buffer:
            continue
        async with lock:
            chunk = "\n".join(buffer); buffer.clear()
            state = load()
            try:
                changes, _writes = await asyncio.to_thread(processor.process_chunk, chunk, state)
            except Exception as e:  # keep the loop alive; show the miss on the board
                state.setdefault("board", {})["lastHeard"] = f"Processor error: {e}"[:200]
            save(state)
            await broadcast(state)
            asyncio.create_task(dispatch(state))


# ---- 3. dispatcher: queued actions → Claude Code (headless) ----------------
async def dispatch(state: dict):
    for a in state.get("actions", []):
        if a.get("status") != "queued":
            continue
        a["status"] = "running"; save(state); await broadcast(state)
        out = RESULTS / f"{a['id']}.html"
        prompt = (f"Task from a live founder call: {a['title']}. Context: {a.get('note','')} "
                  f"Venture: {a.get('venture','')}. Use your tools and the web as needed. "
                  f"Write the finished result as a single self-contained HTML page to {out}.")
        cmd = f"claude -p {shlex.quote(prompt)} --permission-mode acceptEdits --max-turns 30"
        try:
            await asyncio.to_thread(subprocess.run, cmd, shell=True, check=True, timeout=900, cwd=ROOT)
            a["status"] = "done"; a["resultUrl"] = f"/results/{a['id']}.html?t={TOKEN}"
        except Exception as e:
            a["status"] = "failed"; a["note"] = (a.get("note", "") + f" — {e}")[:300]
        save(state); await broadcast(state)


@app.get("/results/{name}")
async def result(name: str, req: Request):
    check_token(req)
    p = RESULTS / name
    if not p.exists() or p.suffix != ".html":
        raise HTTPException(404)
    return FileResponse(p)


# ---- 4. the board -----------------------------------------------------------
@app.get("/events")
async def events(req: Request):
    check_token(req)
    q: asyncio.Queue = asyncio.Queue(); subscribers.add(q)
    q.put_nowait(json.dumps(load()))

    async def gen():
        try:
            while True:
                data = await q.get()
                yield f"data: {data}\n\n"
        finally:
            subscribers.discard(q)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/", response_class=HTMLResponse)
async def board(req: Request):
    check_token(req)
    return (ROOT / "board.html").read_text()


@app.post("/say")
async def say(req: Request):
    """Manual override: POST {"text": "..."} to inject a line (lap keyboard, or testing without a bot)."""
    check_token(req)
    buffer.append((await req.json()).get("text", ""))
    return {"ok": True}


@app.get("/health")
async def health():
    return {"ok": True}


@app.on_event("startup")
async def _start():
    asyncio.create_task(ticker())
