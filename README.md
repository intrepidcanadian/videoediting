# Trailer Maker

**Concept → theatrical trailer, with human-in-the-loop review at every step.**

Local FastAPI app that orchestrates Claude + Nano Banana + Seedance 2.0 + ffmpeg
into a reviewable, resumable trailer-making pipeline. You never sit watching a
black box — every phase has an approval gate where you can edit, regenerate
individual items, or tune parameters before committing to the next (often expensive)
step.

```
concept → ideate → storyboard → assets → keyframes → shots → cut-plan → polish → trailer.mp4
           (opt)    (1 or 3     (logos    (Nano      (Seedance  (Claude    (music   (ffmpeg
                     options)   /chars)    Banana)    2.0)       vision)    +title)  concat)
```

Two entry modes: **From scratch** (Claude writes the storyboard) or **Rip-o-matic**
(upload a reference trailer, we translate its grammar onto your concept).

---

## Quick start

```bash
# 1. Install Python deps (plus ffmpeg for video assembly)
pip install -r requirements.txt
brew install ffmpeg                     # macOS; or apt/equiv elsewhere

# 2. Fill in keys
cp .env.example .env  # if one exists, otherwise edit .env directly
# Required:
#   ARK_API_KEY            — BytePlus Ark (Seedance 2.0)
#   GEMINI_API_KEY         — Google AI Studio (Nano Banana / Gemini image)
#   ANTHROPIC_API_KEY      — Claude (storyboard, review, asset discovery, etc)

# 3. Launch
python server.py
# → http://127.0.0.1:8787
```

Or use the CLI for a fully automated run (no review gates):

```bash
python trailer.py --concept "A lone detective hunts a ghost through 1940s Shanghai" \
                  --shots 6 --ratio 16:9 --title "Shanghai Ghost"
```

---

## The pipeline, phase by phase

Each phase writes to `outputs/<run_id>/` and persists state in `state.json` so
runs are resumable and cloneable.

### 1. ✨ Ideate (optional)
Upload reference images + optional theme/mood → Claude with vision pitches 3
distinct trailer concepts (world, logline, style, suggested shots/ratio). Click
"Use this" to populate the New Trailer form.

### 2. 📝 Storyboard
Claude writes a structured shot list per your concept. Two variants:
- **Single storyboard** (default) — fastest path.
- **3 distinct options** (checkbox) — pick the direction you like from three genuinely different creative takes (opening hook, emotional beat, genre emphasis all varied).

Storyboard returns `title`, `logline`, `character_sheet`, `world_sheet`, and a list of shots, each with `beat`, `duration_s`, `keyframe_prompt`, `motion_prompt`. Claude's system prompt is informed by Google's Nano Banana conventions and the Seedance 2.0 5-part structure (Subject/Action/Camera/Style/Constraints).

You can edit every field inline, or use the ✨ **enhance** button on each prompt to ask Claude to make it more cinematic (context-aware — knows the shot's beat and the run's style).

### 3. 🎯 Asset discovery
Before keyframes render, Claude scans the approved storyboard for concrete assets that need to be *right*:
- Named logos / brand marks
- Specific branded products ("iPhone 15 Pro", not "a phone")
- Named real-world locations
- Character likenesses requiring recognition
- Signature props (a 1967 Impala, specific insignia)
- **Recurring characters when NO reference images were uploaded** — prevents identity drift across shots

Per asset, you get three choices: **Upload** the real thing, **✨ Generate** with Nano Banana (editable prompt), or **Skip**. Handled assets join the reference pool — they're passed to every keyframe that lists them.

### 4. 🖼 Keyframes (Nano Banana)
For each shot, Nano Banana renders a cinematic film still used as Seedance's reference image. References are **labeled** when sent to Gemini so it knows what each image means (*"Reference image 1 — character identity: preserve face, hair, build, wardrobe EXACTLY"* beats an unlabeled dump).

Reference ordering per keyframe:
1. User character refs (strongest identity signal)
2. Shot-specific asset refs (logos, named products, locations)
3. Composition ref (rip-o-matic: source segment's first frame)
4. Prior-shot continuity ref (when no composition ref)

Three actions per keyframe: **↻ regen** (from current prompt), **✏ edit** (Nano Banana surgical edit — "make her hair longer"), **✎ custom** (full prompt override).

### 5. 🎬 Shots (Seedance 2.0)
Each keyframe is animated by Seedance via BytePlus Ark. Renders run concurrently across your `ARK_API_KEYS` pool.

**Scene variants** (rip-o-matic): render 1/2/3 takes per scene. More takes = more material for the timeline to intercut during the review phase. Each take uses a different seed but the same prompt + keyframe ref.

**Camera references per shot** — up to **3 video refs** per Ark spec. You upload short (≤15s) videos and they become `@video1`/`@video2`/`@video3` anchors in the prompt. Seedance uses these to inherit camera motion, pacing, and action choreography (not content).

**Stale detection**: if you edit the keyframe after rendering, the variant gets a `⚠ stale` badge so you know it's out of sync.

### 6. 🔍 Review & Cut Plan (Claude vision)
After shots render, Claude watches every variant's contact sheets and returns:
- Per-shot **quality score 1–10**
- **Defects** (face morphs, garbled text, jittery motion)
- **Cut-in / cut-out** points (where the shot really settles vs. the ramp-up on the front, the stutter on the back)
- **Continuity notes** to the next shot
- **Regenerate recommended** flag → one-click auto-retry up to 2× on flagged shots

**Timeline** (rip-o-matic): we also reconstruct the source trailer's full cut rhythm as a list of slices, each drawn from a scene variant at a proportional time. Takes alternate between slices in the same scene for intercut energy. Timeline is editable — swap variants per slice or adjust cut points. ✨ **Refine with vision** re-runs Claude to pick taste-based variant choices instead of mechanical round-robin.

### 7. 🎵 Polish
- **Music bed** — upload an mp3/wav. librosa extracts BPM + beats + energy spikes + dynamic range. ⚡ **Snap timeline to beats** retimes slice boundaries to land within ±100ms of a beat. Muxed into the final mp4.
- **Title card** — Nano Banana renders a cinematic title still using your run's title. Optional Seedance micro-animation (subtle push + dust particles). Appended to the final stitch.

### 8. 🎞 Stitch
ffmpeg executes the approved cut plan: trims each shot to its cut_in/cut_out (or timeline slices, if present), optional crossfades between shots, appends title card, muxes music. Output: `outputs/<run>/trailer.mp4`.

---

## Key features

| | |
|---|---|
| **Review-as-you-go** | Every phase is an approval gate. Nothing auto-marches to the expensive next step without a click. |
| **Per-item regen** | Any keyframe, shot, variant, or asset can be regenerated individually — rest of the run unaffected. |
| **Nano Banana edit mode** | Surgical edits that preserve composition + identity ("make his coat burgundy"). Automatic backup before overwrite. |
| **Rip-o-matic** | Upload a trailer → ffmpeg scene-detect → Claude translates each segment into your concept's grammar → renders with source segments as auto-attached camera refs. |
| **@image / @video anchors** | ComfyUI-community syntax. Write `"The detective in @image1 walks through @image2"` — Gemini/Seedance weight refs decisively when declared by position. Validation warns about dangling refs. |
| **Prompt rules engine** | 17 default deterministic rules (editable via UI) that normalize model-specific tokens: strip SDXL-era cruft from Nano Banana, transition language from Seedance, clamp Seedance motion prompts to ~400 chars, append the Seedance banlist clause, normalize lens/aperture notation, wrap title-card text in quotes, etc. |
| **Scene variants + timeline cut** | Rip-o-matic renders N takes per scene; a taste-based timeline intercuts slices from those takes to reconstruct the source's rapid-cut rhythm without the cost of overlapping renders. |
| **Vision-refined slicing** | Claude watches every variant's contact sheets + current timeline and swaps in the variant whose composition best matches each slice. |
| **Music beat snap** | librosa beats + energy spikes; timeline slices retime to land on beats within tolerance. Sync score reported. |
| **Asset discovery** | Claude flags named logos/products/locations AND (when you uploaded no refs) the primary recurring character. Per-asset Upload / ✨ Generate / Skip. Joins the reference pool. |
| **Live activity log** | Per-run `log.jsonl`, tailed in a color-coded drawer at the bottom of the run view. Every rule firing, API call, and state change is logged. |
| **Cost tracking** | Every API call (Claude / Nano Banana / Seedance) appended to `costs.jsonl`. Header chip shows running run-total. Prompt caching enabled on all Claude system prompts (~70-90% discount on cached tokens). |
| **Retry on transient** | Seedance + Nano Banana calls retry 3× on 429/502/503/504 + timeouts with exponential backoff. |
| **Clone / archive** | Clone a run (preserves concept/story/refs/assets, clears renders — for ratio/style experiments). Archive bundles a run into a downloadable zip. |

---

## Two modes

### From scratch
Concept → Claude writes storyboard → everything downstream normal.

### 🎞 Rip-o-matic
Upload a reference trailer → we:
1. Scene-detect with ffmpeg (tunable threshold)
2. Clamp segments to 3–12s each, 4–10 total
3. Claude reads each segment's first + last frame with vision + your concept
4. Returns a translated storyboard where each shot stages YOUR subjects in the source's composition
5. Auto-attaches source segments as each shot's `@video1` camera ref
6. Stores the source's full cut timeline for later slice-reconstruction

All later phases work identically — but the **cut plan's timeline** now tries to reconstruct the source's exact cut rhythm using your rendered variants.

---

## Configuration

### `.env`
| Key | What for |
|---|---|
| `ARK_API_KEY` | BytePlus Ark (Seedance). Alternatively `ARK_API_KEYS=k1,k2,...` for concurrent rendering with a key pool. |
| `ARK_BASE_URL` | Defaults to `https://ark.ap-southeast.bytepluses.com/api/v3`. Change to `https://ark.cn-beijing.volces.com/api/v3` for mainland Volcengine. |
| `ARK_MODEL` | Default `dreamina-seedance-2-0-260128`. |
| `GEMINI_API_KEY` | Google AI Studio key. |
| `NANO_BANANA_MODEL` | Default `gemini-2.5-flash-image` (stable). Upgrade to `gemini-3.1-flash-image-preview` for latest quality. |
| `ANTHROPIC_API_KEY` | Claude key. |
| `ANTHROPIC_MODEL` | Default `claude-sonnet-4-6`. |

### `prompt_rules.json` (editable in-UI)
The deterministic prompt transformer. 17 default rules grouped by target (`nano_banana_keyframe`, `nano_banana_edit`, `nano_banana_title`, `nano_banana_asset`, `seedance_motion`). Rule kinds:
- `strip_regex`, `strip_phrases`, `append`, `prepend`, `replace_regex`, `clamp_length`

Edit via the **Rules** tab in the UI (test panel included). Changes live on next API call. Reset-to-defaults one click.

---

## File layout

```
trailermaking/
├── server.py              FastAPI entrypoint (42+ endpoints)
├── pipeline.py            Orchestrator — per-phase functions, state management
├── storyboard.py          Claude storyboard (+ rip translation + multi-option)
├── nano_banana.py         Gemini 2.5 Flash Image — generate + edit mode
├── seedance.py            BytePlus Ark client (retries, pooled keys, multi-video refs)
├── review.py              Claude vision cut plan + timeline refinement
├── assets.py              Asset discovery (logos / products / characters)
├── ideate.py              Concept brainstorm + prompt enhance
├── music.py               librosa beat tracking + snap-to-beats
├── anchors.py             @imageN / @videoN parsing + validation
├── prompt_rules.py        Deterministic prompt transformer engine
├── costs.py               Per-call cost tracking
├── retry.py               Transient-retry decorator
├── logger.py              JSONL per-run logger
├── imgutils.py            Image resize for API uploads
├── video.py               ffmpeg helpers (concat, trim, scene-detect, frame extract, still-to-clip, mux)
├── trailer.py             CLI entrypoint (auto mode, no review gates)
├── prompt_rules.json      Editable rule config
├── static/
│   ├── index.html         Single-page UI
│   └── app.js             UI state machine + live polling
└── outputs/<run_id>/      Per-run state + artifacts
    ├── state.json         Everything about the run
    ├── log.jsonl          Structured activity log
    ├── costs.jsonl        Per-call cost log
    ├── storyboard.json    Latest storyboard
    ├── references/        User-uploaded character/location refs
    ├── assets/            Discovered assets (uploaded or generated)
    ├── keyframes/         Nano Banana stills
    ├── shots/             Seedance mp4s (variants named shot_NN_vM.mp4)
    ├── video_refs/        Per-shot camera refs (up to 3 per shot)
    ├── contact_sheets/    Extracted frames for cut-plan review
    ├── slices/            Trimmed slices used during timeline stitch
    ├── music/             Uploaded audio + librosa analysis
    ├── title/             Title card still + optional animated mp4
    ├── source/            Rip-o-matic source trailer + extracted segments
    └── trailer.mp4        Final output
```

---

## Stack

| Layer | Tool |
|---|---|
| **Orchestration** | FastAPI + asyncio, pipeline.py owns state |
| **LLM reasoning** | Claude Sonnet 4.6 (default), prompt-cached system prompts |
| **Keyframes + edits** | Gemini 2.5 Flash Image ("Nano Banana") |
| **Video generation** | Seedance 2.0 via BytePlus Ark |
| **Scene detection + edit** | ffmpeg (scene filter, trim, concat, mux, xfade, still-to-clip) |
| **Audio analysis** | librosa + soundfile |
| **UI** | Single page HTML + Tailwind CDN + vanilla JS (no build step) |
| **Persistence** | File system — `outputs/<run_id>/state.json` is the source of truth |

---

## Typical costs (April 2026)

| Phase | Per-unit | Notes |
|---|---|---|
| Claude calls | ~$0.003–$0.05 per call | 70–90% cheaper on subsequent calls via prompt caching |
| Nano Banana keyframe / edit / asset | ~$0.04 | 8 refs allowed per call |
| Seedance 2.0 shot (Standard, 1:1, 5s) | ~$0.40 | up to 8 image refs + 3 video refs |
| Title animation (optional) | ~$0.40 | one extra Seedance call |

**6-shot trailer, 2 takes per scene, rip-o-matic with timeline + music + title card, all phases approved:**
~$0.03 (ideate + storyboard + assets + review + refine) + ~$0.24 (6 keyframes) + ~$4.80 (12 shot variants) + ~$0.40 (title card) = **~$5.50 end-to-end**, ~25–40 min wall clock.

Exact per-run spend is tracked and shown in the header chip + `outputs/<run>/costs.jsonl`.

---

## Usage tips

### Best practices (earned through trial)
- **Upload a character portrait as a reference** — even one photo dramatically reduces identity drift across shots. If you don't, asset discovery will flag it and offer to generate one.
- **Start with the ✨ Ideate panel** when you don't have a fully-formed concept. Upload a mood reference + type one vibe word → get three pitchable options.
- **Approve the storyboard carefully** — this is when changes are cheapest. Edit beats, swap prompts, or regenerate the whole thing before spending on keyframes.
- **In rip-o-matic, tune the segmentation threshold** (expandable panel at top of storyboard) if the source's rhythm doesn't match what you see. Lower threshold = more cuts detected.
- **Use `@image1` anchors explicitly in prompts** — `"@image1 walks through @image2's alley"` outperforms `"a detective walks through an alley"` by a wide margin when refs are attached.
- **Check the activity log drawer** — every rule firing, every API call, every error surfaces there. Helps diagnose why a render came out the way it did.
- **Clone a run before experimenting** — try a different ratio, variants count, or style without losing what already worked.

### When things go wrong
- **Keyframe looks nothing like your character** — check that reference images are attached AND mentioned with `@image1` / `@image2` in the prompt. Unlabeled refs have weak pull on Gemini.
- **Shots look great but feel mechanical** — approve the cut plan, then click ✨ **refine with vision** for taste-based variant picks, then upload music and hit ⚡ **snap to beats**.
- **Seedance 502/429 during rendering** — retry is automatic (3× with backoff). If still failing, check your Ark key quota, or add `ARK_API_KEYS=` with multiple keys for rate-limit dodging.
- **Nano Banana 404 on model name** — Google rotates preview models. Set `NANO_BANANA_MODEL=gemini-2.5-flash-image` (stable) in `.env`.

---

## CLI

Headless auto-mode (no approval gates — everything runs to completion):

```bash
python trailer.py \
  --concept "A lone detective hunts a ghost through 1940s Shanghai" \
  --shots 6 \
  --shot-duration 5 \
  --ratio 16:9 \
  --style "Roger Deakins lighting, Dune (2021) palette" \
  --title "Shanghai Ghost" \
  --ref-image portrait.jpg \
  --ref-image location.jpg

# Resume a partial run:
python trailer.py --resume outputs/20260424_153012_shanghai_ghost
```

For anything non-trivial, use the UI — the review gates are the feature.

---

## Development

- Python 3.10+ required (3.9 works but typing quirks).
- `node --check static/app.js` validates the JS bundle parses (no build step).
- Hot-reload: kill the server, edit, relaunch (`lsof -ti:8787 | xargs kill; python server.py`).
- Rules config is a plain JSON file — diff-friendly, version-control it.
- All state is in files; nothing runs against a database. Delete `outputs/<run>/` to forget a run.
# videoediting
