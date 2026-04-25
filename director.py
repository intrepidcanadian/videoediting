"""Director conversation mode — Claude as your collaborator, not your tool.

The user types in natural language: *"the detective looks wrong in shot 3 — make
his coat burgundy"*, *"snap the timeline to the beats"*, *"regen shot 5 take 2"*.
Claude inspects the run state, picks a tool, executes, and replies.

Conversation state lives at state.director_chat (a list of {role, ts, content}).
We cap history to N turns so cost stays bounded.

Tools exposed to Claude:
  - get_state_summary       — compact view of the run's current state
  - regen_shot              — re-render a specific shot variant
  - swap_variant            — promote a variant to primary
  - edit_keyframe           — surgical Nano Banana edit on a keyframe
  - regen_keyframe          — re-render a keyframe (with optional prompt override)
  - edit_vo_line            — change one line's text + resynthesize
  - regen_vo_script         — Claude rewrites the whole VO
  - snap_to_beats           — retime timeline slices to music beats
  - update_timeline_slice   — swap variant or adjust in/out on one slice
  - run_stitch              — re-stitch the final trailer

Keep tool set tight — more tools = more misfires.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

import textutils


def _bg(coro):
    task = asyncio.create_task(coro)
    task.add_done_callback(_log_task_exception)
    return task


def _log_task_exception(task: asyncio.Task):
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        print(f"[director-bg] unhandled exception: {exc}", file=sys.stderr)
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)

load_dotenv(Path(__file__).parent / ".env", override=True)

from constants import ANTHROPIC_MODEL, MAX_TOKENS_DIRECTOR  # noqa: E402

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MAX_TOOL_ITERATIONS = int(os.getenv("DIRECTOR_MAX_ITERATIONS", "6"))
MAX_HISTORY_TURNS = int(os.getenv("DIRECTOR_MAX_HISTORY", "20"))


DIRECTOR_SYSTEM = """You are the director's co-pilot on a trailer production. The user collaborates with you in natural language; you inspect the run's state and execute changes via tools.

Principles:
  - PRESERVE WORK. Never nuke assets without being asked. Regenerate a single shot, edit a single keyframe, swap a single variant — don't rebuild the run.
  - BE NARROW. "The detective looks wrong" → find the specific shot/keyframe, ask which if ambiguous, take the smallest useful action.
  - BE HONEST. You can read the state JSON but you CANNOT see the rendered video itself in this conversation. If the user asks a visual question ("does this shot look good?") and you don't have the answer from state, say so — or suggest they hit the cut-plan review phase which runs Claude-vision over contact sheets.
  - SHORT MESSAGES. This is a working session, not a report. No "Certainly!" / "I'd be happy to". Just do the thing and say what you did.
  - CHAIN TOOLS. If the right move is "edit keyframe 3 then regen shot 3", do both.
  - ASK WHEN AMBIGUOUS. Don't guess between two shots.

The user cares about shipping a trailer that feels like a trailer. Match their energy — terse when they're terse, generous when they want to explore.
"""


# ─── Tool schemas ────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_state_summary",
        "description": "Get a compact summary of the current run state: title, shot count, keyframe/shot statuses, whether music/VO/title-card/cut-plan are attached. Always call this first if you need context you don't already have.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "regen_shot",
        "description": "Re-render a shot variant from scratch via Seedance. Use when a shot is visually broken, has defects, or doesn't match the storyboard beat.",
        "input_schema": {
            "type": "object",
            "properties": {
                "shot_idx": {"type": "integer", "description": "0-indexed shot number"},
                "variant_idx": {"type": "integer", "description": "Which variant (take) to re-render. Defaults to primary/0.", "default": 0},
                "motion_prompt": {"type": "string", "description": "Optional override. If omitted, uses the storyboard's motion_prompt."},
            },
            "required": ["shot_idx"],
        },
    },
    {
        "name": "swap_variant",
        "description": "Set which variant of a shot becomes primary. Only works if that variant is already rendered. Use when the user prefers a different take.",
        "input_schema": {
            "type": "object",
            "properties": {
                "shot_idx": {"type": "integer"},
                "variant_idx": {"type": "integer"},
            },
            "required": ["shot_idx", "variant_idx"],
        },
    },
    {
        "name": "edit_keyframe",
        "description": "Apply a surgical Nano Banana edit to an already-rendered keyframe. Preserves composition and identity — just changes what the edit describes. Example: 'make his coat burgundy'. Backup saved automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "shot_idx": {"type": "integer"},
                "edit_instruction": {"type": "string", "description": "Short, surgical: 'make her hair longer', 'remove the car', 'shift time of day to dusk'."},
            },
            "required": ["shot_idx", "edit_instruction"],
        },
    },
    {
        "name": "regen_keyframe",
        "description": "Re-render a keyframe from scratch (not an edit — a full re-roll). Use when the shot's whole composition is wrong.",
        "input_schema": {
            "type": "object",
            "properties": {
                "shot_idx": {"type": "integer"},
                "keyframe_prompt_override": {"type": "string", "description": "Optional. If omitted, uses the storyboard's keyframe_prompt."},
            },
            "required": ["shot_idx"],
        },
    },
    {
        "name": "edit_vo_line",
        "description": "Change a VO line's text or timing, then resynthesize audio for that line via ElevenLabs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "line_idx": {"type": "integer"},
                "text": {"type": "string", "description": "New text for this line"},
                "start_s": {"type": "number", "description": "Optional — seconds into the trailer where this line enters"},
            },
            "required": ["line_idx", "text"],
        },
    },
    {
        "name": "regen_vo_script",
        "description": "Have Claude rewrite the whole VO script. Use when the user wants a different tone or the current script isn't landing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "vibe": {"type": "string", "description": "Tonal guidance: 'dread', 'triumphant', 'contemplative'."},
            },
        },
    },
    {
        "name": "snap_to_beats",
        "description": "Retime the cut plan's slice timeline to land on music beats. Requires music already attached.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_timeline_slice",
        "description": "Change which variant a slice uses, or adjust its slice_in/slice_out. One slice at a time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slice_idx": {"type": "integer"},
                "variant_idx": {"type": "integer"},
                "slice_in": {"type": "number"},
                "slice_out": {"type": "number"},
            },
            "required": ["slice_idx"],
        },
    },
    {
        "name": "run_stitch",
        "description": "Re-stitch the final trailer. Use after making changes the user wants to see in the output mp4.",
        "input_schema": {
            "type": "object",
            "properties": {
                "crossfade": {"type": "boolean", "default": False},
            },
        },
    },
]


# ─── Tool execution ──────────────────────────────────────────────────────

def _state_summary(state: dict) -> dict:
    """Compact view for tool results — NOT the full state JSON (too big)."""
    story = state.get("story") or {}
    shots = state.get("shots") or []
    kfs = state.get("keyframes") or []
    music = state.get("music") or {}
    vo = (state.get("audio") or {}).get("vo") or {}
    title = state.get("title_card") or {}
    cut_plan = state.get("cut_plan") or {}
    timeline = cut_plan.get("timeline") or {}

    shot_brief = []
    for i, s in enumerate(shots):
        variants = s.get("variants") or []
        primary = s.get("primary_variant", 0)
        ready_takes = [v.get("idx") for v in variants if v.get("status") == "ready"]
        kf = kfs[i] if i < len(kfs) else {}
        shot_brief.append({
            "idx": i,
            "beat": (story.get("shots") or [{}]*99)[i].get("beat") if i < len(story.get("shots") or []) else None,
            "keyframe_status": kf.get("status"),
            "shot_status": s.get("status"),
            "primary_variant": primary,
            "ready_takes": ready_takes,
            "stale": s.get("stale", False),
            "has_video_ref": bool(s.get("video_refs") or s.get("video_ref")),
        })

    return {
        "title": story.get("title"),
        "logline": story.get("logline"),
        "num_shots": len(shots),
        "shots": shot_brief,
        "music": {"attached": bool(music.get("path")), "bpm": music.get("analysis", {}).get("bpm"), "filename": music.get("filename")} if music else None,
        "vo": {"status": vo.get("status"), "line_count": len((vo.get("script") or {}).get("lines") or []), "voice_id": vo.get("voice_id")} if vo else None,
        "title_card": {"attached": bool(title.get("path")), "text": title.get("text")} if title else None,
        "cut_plan": {
            "approved": cut_plan.get("approved", False),
            "has_timeline": bool(timeline.get("entries")),
            "slice_count": len(timeline.get("entries") or []),
            "sync_score": (timeline.get("snap_report") or {}).get("sync_score"),
        },
        "final_rendered": bool(state.get("final")),
    }


async def _dispatch_tool(run_id: str, tool_name: str, tool_input: dict) -> dict:
    """Execute one tool and return a JSON-serializable result dict."""
    import pipeline
    try:
        if tool_name == "get_state_summary":
            return {"ok": True, "summary": _state_summary(pipeline.get_state(run_id))}

        elif tool_name == "regen_shot":
            idx = int(tool_input["shot_idx"])
            variant_idx = int(tool_input.get("variant_idx") or 0)
            motion_prompt = tool_input.get("motion_prompt")
            _bg(pipeline.run_shot(run_id, idx, prompt_override=motion_prompt, variant_idx=variant_idx))
            return {"ok": True, "message": f"queued re-render of shot {idx+1} take {variant_idx+1} (runs ~60-180s)"}

        elif tool_name == "swap_variant":
            idx = int(tool_input["shot_idx"])
            variant_idx = int(tool_input["variant_idx"])
            shot = await pipeline.set_primary_variant(run_id, idx, variant_idx)
            return {"ok": True, "message": f"shot {idx+1} primary variant → take {variant_idx+1}"}

        elif tool_name == "edit_keyframe":
            idx = int(tool_input["shot_idx"])
            edit = tool_input["edit_instruction"]
            _bg(pipeline.edit_keyframe(run_id, idx, edit_prompt=edit))
            return {"ok": True, "message": f"queued Nano Banana edit on keyframe {idx+1}: '{edit[:80]}'. Shot will be marked stale — run regen_shot after if you want the video to match."}

        elif tool_name == "regen_keyframe":
            idx = int(tool_input["shot_idx"])
            prompt = tool_input.get("keyframe_prompt_override")
            _bg(pipeline.run_keyframe(run_id, idx, prompt_override=prompt))
            return {"ok": True, "message": f"queued full re-render of keyframe {idx+1}"}

        elif tool_name == "edit_vo_line":
            line_idx = int(tool_input["line_idx"])
            text = tool_input["text"]
            start_s = tool_input.get("start_s")
            # Read current script, patch the line, save + resynth
            st = pipeline.get_state(run_id)
            vo = (st.get("audio") or {}).get("vo") or {}
            lines = list((vo.get("script") or {}).get("lines") or [])
            if line_idx < 0 or line_idx >= len(lines):
                return {"ok": False, "error": f"no VO line at idx {line_idx}"}
            lines[line_idx] = {**lines[line_idx], "text": text}
            if start_s is not None:
                lines[line_idx]["suggested_start_s"] = float(start_s)
            await pipeline.update_vo_script(run_id, lines=lines)
            _bg(pipeline.synthesize_vo(run_id))
            return {"ok": True, "message": f"VO line {line_idx+1} updated, resynthesizing all lines now"}

        elif tool_name == "regen_vo_script":
            vibe = tool_input.get("vibe")
            _bg(pipeline.generate_vo_script(run_id, vibe=vibe))
            return {"ok": True, "message": f"regenerating VO script{f' with vibe: {vibe}' if vibe else ''}"}

        elif tool_name == "snap_to_beats":
            report = await pipeline.snap_timeline_to_music(run_id)
            return {"ok": True, "message": f"snapped {report['snapped']}/{report['total']} slices, sync score {int(report['sync_score']*100)}%"}

        elif tool_name == "update_timeline_slice":
            slice_idx = int(tool_input["slice_idx"])
            async with pipeline._get_lock(run_id):
                st = pipeline.get_state(run_id)
                cp = st.get("cut_plan") or {}
                tl = cp.get("timeline") or {}
                entries = list(tl.get("entries") or [])
                if slice_idx < 0 or slice_idx >= len(entries):
                    return {"ok": False, "error": f"no slice at idx {slice_idx}"}
                e = dict(entries[slice_idx])
                if "variant_idx" in tool_input: e["variant_idx"] = int(tool_input["variant_idx"])
                if "slice_in" in tool_input: e["slice_in"] = float(tool_input["slice_in"])
                if "slice_out" in tool_input: e["slice_out"] = float(tool_input["slice_out"])
                e["duration"] = max(0.15, e["slice_out"] - e["slice_in"])
                entries[slice_idx] = e
                tl["entries"] = entries
                tl["total_duration"] = round(sum(x["duration"] for x in entries), 3)
                cp["timeline"] = tl
                st["cut_plan"] = cp
                pipeline._save_state(run_id, st)
            return {"ok": True, "message": f"slice {slice_idx+1} updated"}

        elif tool_name == "run_stitch":
            crossfade = bool(tool_input.get("crossfade", False))
            _bg(pipeline.run_stitch(run_id, crossfade=crossfade))
            return {"ok": True, "message": "re-stitching the final trailer (runs ~10-30s)"}

        else:
            return {"ok": False, "error": f"unknown tool: {tool_name}"}

    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__, file=sys.stderr)
        return {"ok": False, "error": f"{tool_name} failed: {str(e)[:500]}"}


# ─── Conversation runner ─────────────────────────────────────────────────

async def handle_message(run_id: str, user_message: str) -> dict:
    """Run one turn: user message → (optionally N tool calls) → final text.
    Returns {reply_text, tool_trace: [{tool, input, result}, ...], updated_state_summary}."""
    import pipeline

    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    from anthropic import Anthropic
    import httpx as _httpx

    client = Anthropic(api_key=ANTHROPIC_API_KEY, timeout=_httpx.Timeout(120.0, connect=30.0))

    # Load history. Note: we build `messages` from the *prior* chat, then append
    # ONLY the enriched current turn (user_message + state summary). The current
    # turn gets added to `chat` after Claude responds so we don't need fragile
    # dedup logic to strip a bare copy from trimmed history.
    state = pipeline.get_state(run_id)
    chat = list(state.get("director_chat") or [])

    # Build conversation history for Claude (trim to last MAX_HISTORY_TURNS)
    trimmed = chat[-MAX_HISTORY_TURNS * 2:]
    messages = []
    for m in trimmed:
        if m["role"] == "user":
            messages.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            # Rebuild assistant content blocks (text + any tool_use records)
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                blocks.append({
                    "type": "tool_use",
                    "id": tc["id"], "name": tc["name"], "input": tc["input"],
                })
            if blocks:
                messages.append({"role": "assistant", "content": blocks})
        elif m["role"] == "tool_result":
            messages.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": m["tool_use_id"], "content": json.dumps(m["result"])}],
            })

    # Append the current turn — user message enriched with fresh state summary
    summary = _state_summary(state)
    messages.append({
        "role": "user",
        "content": user_message + "\n\n---\n[Current state summary: " + json.dumps(summary) + "]",
    })
    # Persist the raw user message to chat history (without the summary block, which
    # is regenerated fresh every turn and shouldn't bloat the stored transcript)
    chat.append({"role": "user", "ts": pipeline._now(), "content": user_message})

    tool_trace: list[dict] = []
    assistant_text_parts: list[str] = []
    assistant_tool_calls: list[dict] = []

    try:
        import taste as _taste
        sys_blocks = _taste.wrap_system_prompt(DIRECTOR_SYSTEM)
    except Exception as e:
        print(f"[director] taste wrapping failed, using plain system prompt: {e}", file=sys.stderr)
        sys_blocks = [{"type": "text", "text": DIRECTOR_SYSTEM, "cache_control": {"type": "ephemeral"}}]

    # Track why the loop ended so the user sees a clear signal when Claude was
    # cut off (vs. actually finished). Default assumes we exited cleanly via `break`.
    truncation_reason: Optional[str] = None
    final_stop_reason: Optional[str] = None

    for iteration in range(MAX_TOOL_ITERATIONS):
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=MAX_TOKENS_DIRECTOR,
            system=sys_blocks,
            messages=messages,
            tools=TOOLS,
        )
        final_stop_reason = resp.stop_reason
        try:
            import costs
            u = resp.usage
            costs.log_text(run_id, model=ANTHROPIC_MODEL, phase="director",
                input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                cached_tokens=getattr(u, "cache_read_input_tokens", 0) or 0)
        except Exception as e: print(f"[costs] director cost log failed: {e}", file=sys.stderr)

        # Parse Claude's content — mix of text and tool_use blocks
        assistant_blocks = resp.content
        # Record it back into conversation as an assistant turn
        messages.append({"role": "assistant", "content": assistant_blocks})

        tool_calls_this_turn = []
        for block in assistant_blocks:
            if getattr(block, "type", None) == "text":
                assistant_text_parts.append(block.text)
            elif getattr(block, "type", None) == "tool_use":
                tool_calls_this_turn.append(block)
                assistant_tool_calls.append({"id": block.id, "name": block.name, "input": block.input})

        if resp.stop_reason == "tool_use" and tool_calls_this_turn:
            # Execute each tool_use, feed results back
            tool_results = []
            for tc in tool_calls_this_turn:
                result = await _dispatch_tool(run_id, tc.name, tc.input)
                tool_trace.append({"tool": tc.name, "input": tc.input, "result": result})
                tool_results.append({"type": "tool_result", "tool_use_id": tc.id, "content": json.dumps(result)})
            messages.append({"role": "user", "content": tool_results})
            # If this was the LAST allowed iteration and Claude still wants to call more
            # tools, the user should know we cut them off before they could act on the
            # tool results.
            if iteration == MAX_TOOL_ITERATIONS - 1:
                truncation_reason = "max_iterations"
            continue
        else:
            # end_turn (clean finish) or max_tokens (Claude ran out of output budget).
            if resp.stop_reason == "max_tokens":
                truncation_reason = "max_tokens"
            break

    reply_text = "\n".join(t.strip() for t in assistant_text_parts if t.strip()) or "(no reply)"

    # Surface a visible banner when Claude was cut off — otherwise a truncated reply
    # looks indistinguishable from a clean one in the UI.
    if truncation_reason == "max_tokens":
        reply_text += (
            "\n\n⚠️ Claude ran out of output budget mid-reply (stop_reason=max_tokens). "
            "Ask again or say 'continue' to pick up where I left off."
        )
    elif truncation_reason == "max_iterations":
        reply_text += (
            f"\n\n⚠️ I hit the tool-use iteration cap ({MAX_TOOL_ITERATIONS}) before finishing. "
            "The last tool results weren't acted on — ask me to continue to pick up."
        )

    # Persist to conversation history
    chat.append({
        "role": "assistant",
        "ts": pipeline._now(),
        "content": reply_text,
        "tool_calls": assistant_tool_calls,
        "tool_trace": tool_trace,
    })
    # Save whichever of the recent tool_result entries were part of this turn (for continuity)
    state = pipeline.get_state(run_id)
    state["director_chat"] = chat
    pipeline._save_state(run_id, state)

    try: client.close()
    except Exception: pass
    return {
        "reply": reply_text,
        "tool_trace": tool_trace,
        "state_summary": _state_summary(state),
    }


def reset_conversation(run_id: str) -> None:
    import pipeline
    state = pipeline.get_state(run_id)
    state["director_chat"] = []
    pipeline._save_state(run_id, state)


def get_conversation(run_id: str) -> list[dict]:
    import pipeline
    state = pipeline.get_state(run_id)
    return state.get("director_chat") or []
