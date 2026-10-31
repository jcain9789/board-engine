"""
Transcript → board processor.

Every tick (~30 s) this takes the new transcript text plus the current board
state and asks Claude for a small, structured set of changes:

  - focus:        which venture the conversation is on now ("move to X")
  - updates:      per-venture summary rewrites, status changes
  - steps:        next steps added / completed / reassigned
  - actions:      commands for the operator ("Claude, run the margin analysis")
  - heard:        one line describing what it acted on (shown on the board)

The model never free-writes to the store. It returns JSON matching the schema
below; `apply()` turns that into store writes. Anything that spends money or
has real-world consequences is queued with status "needs-go" and waits for an
explicit "go" on the call.

Usage (standalone test):
    python processor.py --state board.json --transcript chunk.txt

In the server, `process_chunk()` is called from the webhook handler.
"""
from __future__ import annotations

import json
import os
import sys
import argparse
from datetime import datetime, timezone

MODEL = os.environ.get("BOARD_MODEL", "claude-sonnet-5-5")  # fast tier is enough here

SYSTEM = """You are the operator behind a live venture board that two founders (John and Paul) look at while they talk on a call. You receive the last ~30 seconds of transcript plus the board's current state. Your job is to keep the board true to the conversation with the fewest possible changes.

Rules:
- Only change what the transcript clearly supports. If nothing relevant was said, return empty lists.
- "move to X", "switch to X", "let's talk about X", "back to X" → set focus to that venture id (fuzzy-match names and aliases).
- When a speaker finishes describing an idea or a decision about the focused venture, rewrite that venture's summary so it stays a tight running summary (2–5 sentences, present tense, no filler). Preserve facts not contradicted by the new transcript.
- Next steps: add one when someone assigns or commits to a task ("Paul, can you…", "I'll…", "we need to…"). Owner is John, Paul, Claude, or a named person; "I'll" resolves to the current speaker when the transcript has speaker labels. Mark a step done when they say it's done. Keep step text under 12 words, imperative.
- Status: "active" (being worked), "exploring" (deciding), "parked" (shelved), "blocked", "done". Change it only when they say so ("park this", "this is live now").
- Actions: when they address Claude directly ("Claude, …", "can you pull…", "run…", "open…", "register…"), create an action with a clear imperative title. Set needs_go=true for anything that files, pays, sends, or publishes externally; false for research, analysis, drafts and spreadsheets.
- If they describe a venture that is not on the board, create it (new_ventures) with a short tagline and status "exploring".
- heard: one short line, past tense, saying what you did ("Added two next steps for Paul on Hands and Feet", "Switched to Suzu"). Empty string if you did nothing.

Return only JSON matching the provided schema."""

SCHEMA = {
    "type": "object",
    "properties": {
        "focus": {"type": ["string", "null"], "description": "venture id to focus, or null for no change"},
        "updates": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"},
            "summary": {"type": ["string", "null"]},
            "status": {"type": ["string", "null"], "enum": ["active", "exploring", "parked", "blocked", "done", None]},
        }, "required": ["id"]}},
        "steps": {"type": "array", "items": {"type": "object", "properties": {
            "venture": {"type": "string"},
            "op": {"type": "string", "enum": ["add", "done", "reopen", "reassign", "remove"]},
            "text": {"type": ["string", "null"]},
            "owner": {"type": ["string", "null"]},
            "match": {"type": ["string", "null"], "description": "for done/reopen/reassign/remove: a few words from the existing step text"},
        }, "required": ["venture", "op"]}},
        "actions": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"},
            "venture": {"type": ["string", "null"]},
            "detail": {"type": ["string", "null"]},
            "needs_go": {"type": "boolean"},
        }, "required": ["title", "needs_go"]}},
        "new_ventures": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "name": {"type": "string"}, "tagline": {"type": "string"}, "summary": {"type": "string"},
        }, "required": ["id", "name", "tagline", "summary"]}},
        "heard": {"type": "string"},
    },
    "required": ["focus", "updates", "steps", "actions", "new_ventures", "heard"],
}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def board_digest(state: dict) -> str:
    """Compact view of the board for the prompt: ids, names, status, summary, open steps."""
    lines = [f"Focused venture: {state.get('board', {}).get('active')}"]
    for v in state.get("ventures", []):
        lines.append(f"\n[{v['id']}] {v['name']} — {v.get('status','exploring')}")
        if v.get("tagline"):
            lines.append(f"  tagline: {v['tagline']}")
        if v.get("summary"):
            lines.append(f"  summary: {v['summary']}")
        for s in v.get("nextSteps", []):
            lines.append(f"  - [{'x' if s.get('done') else ' '}] {s['text']} ({s.get('owner') or 'unassigned'})")
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (tolerates code fences / prose)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


def ask_claude(transcript: str, state: dict, client=None) -> dict:
    """Call Claude and parse a JSON object matching SCHEMA from its reply."""
    if client is None:
        import anthropic  # pip install anthropic
        client = anthropic.Anthropic()
    schema_txt = json.dumps(SCHEMA, indent=0)
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=SYSTEM + "\n\nOutput format: reply with ONE JSON object and nothing else, matching this JSON schema exactly:\n" + schema_txt,
        messages=[
            {"role": "user", "content": f"BOARD STATE\n{board_digest(state)}\n\nTRANSCRIPT (newest 30s)\n{transcript.strip()}"},
            {"role": "assistant", "content": "{"},
        ],
    )
    text = "".join(getattr(b, "text", "") for b in msg.content)
    out = _extract_json("{" + text)
    for k, default in (("focus", None), ("updates", []), ("steps", []), ("actions", []), ("new_ventures", []), ("heard", "")):
        out.setdefault(k, default)
    return out


def apply(changes: dict, state: dict) -> list[dict]:
    """Turn model output into store writes. Returns a list of
    {collection, doc_id, data} full-document writes (idempotent 'set's).
    Mutates `state` to match."""
    writes: list[dict] = []
    ts = now_iso()
    by_id = {v["id"]: v for v in state.setdefault("ventures", [])}

    for nv in changes.get("new_ventures", []):
        if nv["id"] in by_id:
            continue
        v = {"id": nv["id"], "name": nv["name"], "tagline": nv["tagline"], "summary": nv["summary"],
             "status": "exploring", "order": 50, "nextSteps": [], "updatedAt": ts, "updatedBy": "call"}
        state["ventures"].append(v); by_id[v["id"]] = v

    touched: set[str] = set()
    for u in changes.get("updates", []):
        v = by_id.get(u["id"])
        if not v:
            continue
        if u.get("summary"):
            v["summary"] = u["summary"]
        if u.get("status"):
            v["status"] = u["status"]
        touched.add(v["id"])

    for s in changes.get("steps", []):
        v = by_id.get(s["venture"])
        if not v:
            continue
        steps = v.setdefault("nextSteps", [])
        if s["op"] == "add" and s.get("text"):
            steps.append({"id": f"{v['id']}-{len(steps)+1}-{int(datetime.now().timestamp())%100000}",
                          "text": s["text"], "owner": s.get("owner") or "", "done": False})
        else:
            key = (s.get("match") or s.get("text") or "").lower()
            hit = next((st for st in steps if key and key in st["text"].lower()), None)
            if hit is None:
                continue
            if s["op"] == "done":
                hit["done"] = True
            elif s["op"] == "reopen":
                hit["done"] = False
            elif s["op"] == "reassign" and s.get("owner"):
                hit["owner"] = s["owner"]
            elif s["op"] == "remove":
                steps.remove(hit)
        touched.add(v["id"])

    for vid in touched:
        v = by_id[vid]; v["updatedAt"] = ts; v["updatedBy"] = "call"
        data = {k: v[k] for k in v if k != "id"}
        writes.append({"collection": "ventures", "doc_id": vid, "data": data})

    board = state.setdefault("board", {})
    board_changed = False
    if changes.get("focus") and changes["focus"] in by_id and changes["focus"] != board.get("active"):
        board["active"] = changes["focus"]; board["activeSince"] = ts; board_changed = True
    if changes.get("heard"):
        board["lastHeard"] = changes["heard"]; board_changed = True
    if board_changed:
        writes.append({"collection": "board", "doc_id": "state", "data": dict(board)})

    for a in changes.get("actions", []):
        aid = "a-" + datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3]
        doc = {"title": a["title"], "venture": by_id.get(a.get("venture") or "", {}).get("name", a.get("venture") or ""),
               "note": a.get("detail") or "", "status": "needs-go" if a.get("needs_go") else "queued", "createdAt": ts}
        state.setdefault("actions", []).append({"id": aid, **doc})
        writes.append({"collection": "actions", "doc_id": aid, "data": doc})
    return writes


def process_chunk(transcript: str, state: dict, client=None) -> tuple[dict, list[dict]]:
    changes = ask_claude(transcript, state, client)
    return changes, apply(changes, state)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True, help="JSON file with {ventures:[...], board:{...}, actions:[...]}")
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--dry", action="store_true", help="print changes only")
    args = ap.parse_args()
    state = json.load(open(args.state))
    transcript = open(args.transcript).read()
    changes, writes = process_chunk(transcript, state)
    print(json.dumps(changes, indent=1))
    if not args.dry:
        json.dump(state, open(args.state, "w"), indent=1)
        print(f"\n{len(writes)} store write(s)", file=sys.stderr)
