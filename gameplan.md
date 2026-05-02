# Gameplan — shorts_strategist

## Context

This project is the **deep-think reasoning service** for SimpleAutoSubs. It's
a sibling of two existing projects:

- `SimpleAutoSubs` (port 9020) — cuts raw screen recordings into finished
  YouTube Shorts with subtitles, animations, and onomatopoeia.
- `shorts_analyzer` (port 9021) — studies a channel's *published* shorts with
  Gemini to figure out what works on a per-channel basis (synthesis +
  tailwind hypotheses).

`shorts_strategist` runs on **port 9022**. It's registered with the hub
in `youtube_hub/service_defs.py` as `shorts_strategist_api` (conda env
`shorts-strategist`). The hub supervises all three.

The strategist exists because both siblings make single-shot LLM calls
today. There's no place that **iterates, critiques, or cross-references**
channel-level patterns against per-clip decisions. That's the gap this
project fills.

## What this project owns vs. what it does NOT

**Owns:**
- Multi-pass reasoning (generate → critique → select) for titles, cut
  plans, and special effects.
- Editing-strategy memory: a SQLite store joining SimpleAutoSubs's edit
  decisions to shorts_analyzer's outcome data, plus a reasoning layer over
  the join (`patterns.json`).
- Experiment designer: proposes hypothesis-driven A/B tests across upcoming
  recordings, including which videos go in which arm.
- Capability roadmap: reasons about what SimpleAutoSubs *can't do yet* but
  should, based on directives it had to drop.
- Reasoning trace storage in `traces/` for offline prompt iteration.

**Does NOT own:**
- Channel-level corpus reasoning (`synthesis.json`, `tailwind.json`) —
  that's `shorts_analyzer`'s job. The strategist *consumes* those files via
  the analyzer's `/results/read` endpoint.
- Cut execution (FFmpeg, animations, subtitle embedding) — that's
  `SimpleAutoSubs`. The strategist returns *directives*; the subtitler
  applies what it can.
- Publishing — that's `youtube_shorts_publisher`.

## The three pillars

### Pillar 1 — `/think/*` per-clip reasoning

Replaces the inline single-shot Gemini calls SimpleAutoSubs makes today
with multi-pass reasoning. Each endpoint follows the same shape:

1. Pull channel context: cached `synthesis.json` and learned patterns
   (Pillar 2) for the channel.
2. **Generate** a set of candidates with Gemini 3 Pro (`thinking_level=high`).
3. **Critique** them with a different model (Claude Opus) — different model
   choice gives genuinely independent critique, not echo-chamber agreement.
4. **Select** the best with reasoning preserved.
5. Persist the full chain to `traces/<trace_id>.json`.

Endpoints:
- `POST /think/title` — title generation (replaces
  `SimpleAutoSubs/title_generator.py`'s single call).
- `POST /think/cut-plan` — full-clip narrative plan: hook placement, pacing
  beats, recommended trim points with reasoning.
- `POST /think/effects` — per-moment special-effects directives matched
  against the channel's effect vocabulary.

### Pillar 2 — strategy memory (`/strategy/*`)

SQLite at `data/strategy.db`. Schema in
`strategist/strategy.py`:
- `cuts(cut_id, channel_handle, video_id, edit_decisions, title,
  title_reasoning)` — what SimpleAutoSubs decided.
- `outcomes(video_id, breakout_score, retention_curve, raw)` — what
  shorts_analyzer eventually measured.
- `experiments(experiment_id, hypothesis, arms, status, conclusion)` —
  designed and concluded experiments.

Joined by `video_id`, which only gets stamped *after publish*. This is the
critical seam — without `cut_id` flowing through SimpleAutoSubs and
`youtube_shorts_publisher`, the join is empty and Pillar 2 is dead.

Endpoints:
- `POST /strategy/ingest` — called by SimpleAutoSubs (or hub) at cut time.
- `POST /strategy/stamp-video-id` — called by the publisher at publish time.
- `GET /strategy/cuts?channel_handle=...` — raw join, for inspection.
- `GET /strategy/patterns?channel_handle=...` — LLM-reasoned patterns over
  the join, cached as `output/<channel>.patterns.json`.

### Pillar 3 — experiment designer + roadmap (`/experiment/*`, `/roadmap/*`)

The strategist piece that justifies the name. Two distinct features:

**Experiment designer.** Multi-pass reasoning over upcoming recordings:
generate hypothesis candidates → critique for confounds → pick the one
with the best signal/effort ratio → assign videos to arms with
`edit_directives` that override SimpleAutoSubs's defaults. SimpleAutoSubs
must accept a `directives` field on its `/process` endpoint for this to
work end-to-end.

**Capability roadmap.** Reasons over (a) directives from `/think/effects`
that SimpleAutoSubs *couldn't apply*, (b) patterns from Pillar 2 that
imply missing capabilities, and (c) a capability manifest published by
SimpleAutoSubs. Output is a ranked feature backlog written to
`output/roadmap.md`.

Endpoints:
- `POST /experiment/design` — produces an experiment plan.
- `GET /experiment/list` — lists designed experiments.
- `POST /experiment/conclude` (future) — declares an experiment done and
  updates `patterns.json`.
- `POST /roadmap/gaps` — produces the feature backlog.

## Build sequencing

| Phase | What | Why now |
|------|------|---------|
| 0 | Scaffold + hub registration + smoke test | done — see this repo |
| 1 | `/think/title` end-to-end (generate-then-critique) | proves the multi-pass pattern; ships visible win in the hub UI |
| 2 | `cut_id` plumbing through SimpleAutoSubs + publisher | hard prerequisite for Pillar 2; without it everything else is theoretical |
| 3 | `/strategy/ingest` + `/strategy/stamp-video-id` wiring + `/strategy/patterns` | activates the feedback loop |
| 4 | `/think/cut-plan` + `/think/effects` | consumes patterns from Phase 3 |
| 5 | `directives` field on SimpleAutoSubs `/process` + `/experiment/design` | unlocks A/B testing |
| 6 | Capability manifest in SimpleAutoSubs + `/roadmap/gaps` | recommends what to build next |

Phases 1–3 are the critical path. Phases 4–6 can be tackled in any order
once 3 is solid.

## Hard rules

- **Never crash SimpleAutoSubs.** Every strategist call from the subtitler
  must time out fast (<3s for sync calls, async otherwise) and degrade
  gracefully if 9022 is down — same posture SimpleAutoSubs already takes
  with the analyzer.
- **No fallback titles.** The gameplan in `SimpleAutoSubs/gameplan.md`
  states: if title generation fails, the title field is omitted entirely.
  The strategist must surface clear error states so the operator can see
  *why* a field is missing — never invent placeholder text.
- **Two models, not one.** Gemini for generation (vision capability,
  fast), Claude for critique (different family = different blind spots).
  If only one is available, the endpoint should still work but record in
  the trace that critique was skipped.
- **Always write a trace.** Every `/think/*` call writes a trace, even on
  failure. This is the ground truth for prompt iteration.
- **Don't auto-trigger expensive analyzer reruns.** Same rule as
  `SimpleAutoSubs/gameplan.md`: `/rerun/*` on the analyzer is
  operator-triggered. The strategist only consumes `/results/read` and
  `/videos`.
- **The `cut_id` is canonical.** SimpleAutoSubs generates it, the
  publisher stamps `video_id` onto it later. Don't introduce a competing
  identifier.

## What stays out of scope

- **Replacing the intelligent trimmer.** `clip_editor/intelligent_trimmer.py`
  in SimpleAutoSubs makes decisions from raw video; the strategist
  re-ranks its candidates. Phase 4 is a re-ranking layer, not a
  replacement.
- **Auto-running experiments.** The strategist *designs* experiments; it
  does not unilaterally route SimpleAutoSubs traffic into arms. The
  operator (or hub) approves the plan and assigns directives.
- **Channel-level analysis.** If the strategist needs corpus-level
  reasoning, it requests a synthesis rerun from the analyzer (or asks the
  operator to). It does not duplicate that logic.
- **Thumbnail generation.** Out of scope here, same as in the
  SimpleAutoSubs gameplan.

## File map

```
api_server.py                    FastAPI on 9022, all routes
config.py                        Ports, paths, model names, env overrides
main.py                          CLI entry → uvicorn
requirements.txt                 google-genai, anthropic, fastapi, requests
strategist/
    strategy.py                  SQLite schema + CRUD for cuts/outcomes/experiments
    traces.py                    Reasoning-trace storage (traces/<id>.json)
    analyzer_client.py           Graceful client for shorts_analyzer @ 9021
    reasoning.py                 generate-then-critique primitive
data/strategy.db                 SQLite store (gitignored)
traces/                          Per-call reasoning traces (gitignored)
output/                          patterns.json, roadmap.md (gitignored)
```

## Done when

The strategist is "complete enough to be load-bearing" when:

1. SimpleAutoSubs's `title_generator.py` calls `/think/title` and writes
   the returned title into `shorts_data/shorts_metadata_N.json`, with a
   trace_id alongside for debugging.
2. The publisher posts `/strategy/stamp-video-id` after a successful
   upload, populating the join.
3. `/strategy/patterns?channel_handle=PeepingOtter` returns non-trivial
   patterns reasoned over at least ~10 cuts with outcomes.
4. At least one `/experiment/design` plan has been executed end-to-end and
   concluded, updating `patterns.json` with a learned signal.

Anything beyond that is iteration on prompt quality and the capability
roadmap.
