# Board Engine — Mac-free live venture board

A small server that turns a working call into live board updates, with Claude as the operator.

## What's here
- `board.html` — the board (same design as the claude.ai artifact, fed by server-sent events). Open it in Vision Pro Safari and pin the window.
- `processor.py` — transcript → structured board changes via Claude (forced JSON tool). Test it alone with `--dry`.
- `server.py` — FastAPI app: `/transcript` webhook for the meeting bot, 30-second tick that runs the processor, `/events` SSE feed, `/say` manual injection, and a dispatcher that hands queued actions to Claude Code (`claude -p`) and serves the results.
- `state.json` — the board's data. Seeded with the 11 ventures as of Oct 31.

## Deploy (any small box: Fly.io, Railway, a $5 VPS)
```
pip install fastapi uvicorn anthropic
export ANTHROPIC_API_KEY=...          # processor
export BOARD_TOKEN=some-long-secret   # protects the board URL and SSE feed
export RECALL_WEBHOOK_SECRET=...      # optional, if the bot signs webhooks
uvicorn server:app --host 0.0.0.0 --port 8080
```
Install Claude Code on the same box and log it in so the dispatcher can run actions with your tools (`claude mcp add ...` for anything you want it to reach).

Board URL for you and Paul: `https://<host>/?t=<BOARD_TOKEN>`

## Listener (meeting bot)
Create a Recall.ai bot per call (or use their calendar auto-join) with real-time transcription on, and point its webhook at `https://<host>/transcript`. The handler expects `transcript.data` events (speaker + words); the adapter is six lines in `server.py` if you use a different bot.

## Testing without a call
```
curl -X POST "https://<host>/say?t=<BOARD_TOKEN>" -H 'content-type: application/json' \
  -d '{"text":"John: Paul, can you get quotes from three node accounts by Friday? Then let us move to Suzu."}'
```
Within 30 s the board should add the step for Paul and switch focus to Suzu.

## Safety rails
- Anything that files, pays, sends or publishes is queued as **needs go** and does nothing until someone says "go" on the call (the processor flips it to `queued`).
- The processor only ever returns structured changes; it cannot free-write the store.
- Tapping a card or a step on the board injects a plain-English line into the same pipeline, so manual corrections and voice go through one path.

## Next
1. Deploy and smoke-test with `/say`.
2. Connect the meeting bot; run one real call.
3. Teach the dispatcher to also publish results as claude.ai artifacts (optional; results are already viewable in-headset from the server).
