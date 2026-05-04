# Gameplan — shorts_strategist

## Context

This project is the **deep-think reasoning service** for the YouTube
shorts pipeline. It's a sibling of two existing projects:

- `SimpleAutoSubs` (port 9020) — cuts raw screen recordings into finished
  YouTube Shorts with subtitles, animations, and onomatopoeia.
- `shorts_analyzer` (port 9021) — studies a channel's *published* shorts
  with Gemini to figure out what works on a per-channel basis (synthesis
  + tailwind hypotheses).

`shorts_strategist` runs on **port 9022** under `service_defs.py` as
`shorts_strategist_api`. The hub supervises all three.

The siblings make single-shot LLM calls. The strategist exists because
nothing else **iterates, critiques, cross-references, or watches for
drift over time**. That's the gap this project fills.

## What this project produces

The strategist runs a **continuous background thinker** that reads
analyzer + simpleautosubs outputs and writes opinionated recommendations
to `output/recommendations/`. Four product categories:

1. **Pre-publish title recommendations** (`output/recommendations/titles/<base>.json`)
   — for every clip simpleautosubs has already prepared but hasn't shipped,
   verdict (keep / replace / tied) plus 5 ranked alternatives, each citing
   which channel patterns it uses.

2. **Capability/editing ideas for simpleautosubs** (`output/recommendations/capabilities/<handle>.json`)
   — short, opinionated build backlog (top 3, up to 5) of editing
   capabilities that correlate with breakouts but simpleautosubs doesn't
   have. Each gap cites evidence (lift × n × avg breakout), proposes a
   concrete simpleautosubs module to build it in, and includes an
   experiment to validate. Also surfaces a "do NOT build" list of
   capabilities the data shows would hurt (PIP facecam, kinetic text on
   subtitles, etc).

3. **Pre-publish edit review with iteration loop**
   (`output/recommendations/edits/<base>.json`) — for each pre-publish
   clip, scores SimpleAutoSubs's cut against channel patterns + the
   capability manifest, and either recommends specific edit directives
   (cinematography / onomatopoeia / retrim / subtitle) or signals
   which iteration to ship. SimpleAutoSubs owns the iteration counter
   (max 3) and enforces it; the strategist picks the best of the
   iterations it has seen. *Strategist side shipped; runs in
   advisory-only mode until SimpleAutoSubs ships the
   `editorial_decisions` metadata extension. See "Pre-publish edit-review
   feedback loop" below for the wire format.*

4. **Channel-level scorecard** (`output/recommendations/channel/<handle>.scorecard.json`)
   — opinionated read of "what's working / what isn't / what to do next"
   with a headline, prioritized actions (each critiqued for evidence and
   sample size), tag-drift table, conditional levers, monthly trajectory,
   and tailwind caveats.

A supporting **title-pattern retrospective** (`output/recommendations/channel/<handle>.title_patterns.json`)
runs first; it's the dependency that grounds title recs and scorecard
references.

## Architecture

### The thinker loop

`strategist/thinker.py` runs a single background worker. On each tick:

1. Build a snapshot of all inputs (`strategist/inputs.py`) — analyzer per-short,
   tailwind, synthesis, context + simpleautosubs pre-publish files. Each
   source is hashed (sha256) so changes are detectable.
2. Enumerate every task module in `strategist/tasks/REGISTRY`, asking
   each to produce its task list given the snapshot.
3. For every task, hash its inputs and compare to the artifact already on
   disk. Skip if unchanged.
4. Topo-sort the stale tasks by their declared `depends_on` and run **one
   per tick**. Idle when caught up; sleep until inputs change or an
   operator forces a re-run.

State persists to `data/thinker_state.json` so the UI shows continuity
across restarts (last tick, session task count, error log).

### Multi-pass reasoning

Every task that calls an LLM uses **two models** for genuine independent
critique:

- **Generate** with Gemini (vision-capable, fast)
- **Critique** with Claude (different family = different blind spots)
- **Merge / select** in Python — drop critic-rejected items, swap in
  narrowed descriptions for "weaken" verdicts, surface critic-added
  items the generator missed

Every call writes a trace to `traces/<trace_id>.json`. Artifacts carry
the `trace_id` so the UI can link back to the reasoning.

Cost guardrails: each task hashes its inputs and skips if the artifact's
`input_hash` matches. Force-rerun (UI ↻ button) bypasses the hash.

### File map

```
api_server.py                          FastAPI on 9022
config.py                              Ports, sibling paths, model names
main.py                                CLI entry → uvicorn
strategist/
    inputs.py                          Tick-snapshot of all input sources
    recommendations.py                 Atomic-write artifact store
    thinker.py                         Background worker + DAG topo sort
    llm.py                             Gemini + Claude clients (graceful)
    reasoning.py                       generate-then-critique primitive
    traces.py                          Per-call reasoning traces
    analyzer_client.py                 Graceful client for shorts_analyzer
    logs.py                            Tee stdout/stderr for /logs endpoint
    tasks/
        base.py                        Task ABC + dep typing
        title_pattern_retro.py         Corpus → use/avoid title patterns
        pre_publish_title.py           Per pre-publish file → ranked alternatives
        channel_scorecard.py           Pre-compute tables + LLM headline/actions
        capability_gaps.py             Manifest × analyzer signals → top-3 build backlog
        pre_publish_edit_review.py     Per pre-publish file → score + edit directives + ship pick
data/
    thinker_state.json                 Persisted thinker state (gitignored)
    capability_manifest.json           Strategist-side fixture of what simpleautosubs can do
output/recommendations/
    channel/<handle>.title_patterns.json
    channel/<handle>.scorecard.json
    titles/<base>.json
    capabilities/<handle>.capability_gaps.json
    edits/<base>.json
    postmortems/<video_id>.json        (future, if built)
traces/<trace_id>.json                 (gitignored)
```

### API surface

| Endpoint | Purpose |
|---|---|
| `GET /thinker/status` | State (running/idle/stopped/error), queue depth, current task, errors |
| `POST /thinker/start` / `/stop` | Lifecycle control |
| `POST /thinker/force` | Mark a task stale to bump it to the front of the next tick |
| `GET /thinker/queue` | Pending tasks in topo order |
| `GET /recommendations/categories` | List the canonical category names |
| `GET /recommendations/{cat}` | List artifacts in a category |
| `GET /recommendations/{cat}/{key}` | Read one artifact |
| `GET /traces` / `/traces/{id}` | Reasoning-trace inspection |
| `GET /logs` / `DELETE /logs` | Console tail for the hub |
| `GET /health` | Service + analyzer reachability |

The `/think/title`, `/strategy/*`, `/experiment/*`, and `/roadmap/*`
endpoints from the original gameplan still exist in the API but are
**cold paths** today. They were the per-call architecture; the thinker
loop is the architecture that's actually load-bearing. Future cleanup
should retire them rather than build on them.

### Hub UI

The strategist tab opens on a **Thinker** panel (cuts/experiments/traces
tabs preserved as deep-debug). Thinker panel shows:

- Status pill, queue depth, last tick, session task counts
- Start / Stop buttons
- Current task + input snapshot summary
- Error log
- Pending queue preview
- **Recommendations browser** with a category nav (Channel / Titles /
  Postmortems / Tailwind critiques / Capability gaps)

Each artifact opens into a **structured per-task-type renderer**:

- **Channel scorecard** — headline + critique callouts, prioritized
  actions as cards with verdict-colored borders, drift table with
  3-column horizontal bars (top% / bottom% / recent%), conditional
  levers with multiplier callouts, mini sparkline trajectory, tailwind
  caveats, critic-added actions, experiment substrates, open questions.
- **Title rec** — verdict banner, current title with patterns_uses /
  patterns_violates pills, 5 ranked alternative cards (recommended one
  highlighted), critic scores + accuracy concerns inline.
- **Title patterns** — two-column use/avoid grid with median multiplier
  pills, evidence titles, critic verdict per pattern, missing-patterns
  flagged by critic.
- **Capability gaps** — top-3 build-proposal cards with effort badge,
  evidence row (lift / n / avg breakout), implementation module callout,
  and concrete validation experiment. Below: a "Do NOT build" grid
  visualizing top% vs bottom% bars for negative-signal tags.
- **Edit review** — verdict banner (ship_current / ship_prior / needs_edits),
  iteration progress (N of max), score pill + breakdown, concerns list,
  edit directives grouped by type (cinematography / onomatopoeia /
  retrim / subtitle) with timestamp + action callouts, iteration history
  with the best version highlighted. Falls back to a pattern-level
  checklist when SimpleAutoSubs hasn't yet shipped editorial_decisions.

Every viewer has a **Raw JSON** toggle so the source artifact is always
one click away. A **partial-critique** banner appears when the critic
returned fewer items than the generator proposed.

## What needs improvement (in current artifacts)

These are real issues caught by reading recent output, not theoretical
concerns:

1. **Critic-flagged missing patterns are second-class.** In
   `title_pattern_retro` the critic surfaces 3-4 patterns the generator
   missed (e.g. "He/My Team [verb]ed me" generic phrasing underperforms),
   but those land in `missing_patterns_flagged_by_critic` as plain
   strings — never reaching `pre_publish_title`'s consumed
   `patterns_to_use`/`patterns_to_avoid`. Worth promoting on a re-run, or
   feeding them as additional candidates next time the retrospective
   fires.

2. **Multiplier inflation isn't visualized.** Conditional-lever cards
   show "300×" prominently when the math comes from `mean_alone = 0.007`.
   The critic catches it in text ("absurdly low baselines, multipliers
   mathematically inflated rather than meaningful"), but a hurried reader
   might not. Add a "thin baseline" badge on lever cards where
   `mean_alone < ~0.05`. The CSS already dims `n_combined < 5`; this
   would extend the same warning posture to the baseline axis.

3. **Scorecard presents competing causal stories without resolution.**
   The headline blames packaging drift; the critic surfaces a
   publishing-volume confound (April 2026: 36 shorts vs typical 2-6).
   Both could be true. The scorecard should pick a leading hypothesis
   *or* propose an experiment that disambiguates, rather than presenting
   both side-by-side without a verdict.

4. **No cost gate.** A force-rerun-all on a saturated queue could spend
   an unbounded number of LLM dollars. Worth adding `MAX_TASKS_PER_SESSION`
   or a hard $ ceiling that pauses the loop with a visible flag.

5. **Title-rec test surface is narrow.** Current 3 pre-publish files are
   variants of the same gag, so we can't tell how the system handles
   diverse content. Expanding the pre-publish corpus (or fabricating
   fixtures) would surface real failure modes.

## What needs to be built

### New task types (in priority order)

- **`reedit_review`** — per published short. Takes the analyzer's per-video
  analysis + outcome data and proposes specific re-cuts / effect
  additions. Advisory only until simpleautosubs accepts re-edit directives.
  Cost: ~$0.50/short → gate to top + bottom quintiles only (~50 videos).

- **`synthesis_critique`** — corpus task. Second pair of eyes on the
  analyzer's narrative; cheap (~$0.20). The scorecard already does most
  of this work, so build only if the scorecard's authority needs
  bolstering.

- **`postmortem`** — per video_id. Why each short over/underperformed
  vs channel + monthly medians. Big batch (125 tasks, ~$10-15 first
  run). Consider gating to top + bottom quintile (~50). Skip entirely
  unless there's a use case the scorecard doesn't already cover.

### Pre-publish edit-review feedback loop (in flight)

`pre_publish_edit_review` is the strategist-side task that closes the
edit feedback loop with SimpleAutoSubs. The strategist scores each
iteration and either recommends edit directives (cinematography,
onomatopoeia, retrim, subtitle) or signals which iteration to ship.

**Wire format — `shorts_metadata_<n>.json` extension (SimpleAutoSubs writes):**

```json
{
  "file_info": {
    "iteration": 1,                       // current iteration; 1-based
    "max_iterations": 3,                  // SimpleAutoSubs owns + enforces
    "iteration_history": [
      {
        "iteration": 1,
        "output_filename": "...-v1.mp4",
        "trigger": "initial_cut" | "strategist_directive_vN",
        "editorial_decisions": { … }      // editorial decisions for this iteration
      }
    ]
  },
  "title": "…",
  "title_analysis": { … },
  "editorial_decisions": {
    "trim_segments_kept":   [[5.0, 12.5], [22.0, 30.0]],
    "trim_punch_point":     28.5,
    "zoom_timeline":        [{ "time": 6.0, "action": "zoom_to_game", "duration": 2.5 }, …],
    "onomatopoeia_events":  [{ "time": 28.0, "word": "BLAM", "animation": "explode_out", "intensity": 0.85 }, …]
  }
}
```

**Wire format — `output/recommendations/edits/<base>.json` (strategist writes):**

```json
{
  "verdict": "needs_edits" | "ship_current" | "ship_prior",
  "iteration_reviewed": 1,
  "max_iterations_known": 3,
  "ship_which_iteration": 2,            // present when verdict starts with "ship"
  "current_score": 62,
  "edit_directives": {                  // present only when verdict == "needs_edits"
    "retrim": { "should_retrim": false, "new_segments_to_keep": null, "reason": null },
    "cinematography_changes": [
      { "type": "add"|"remove"|"replace", "at_time": 24.0,
        "from_action": "<zoom action or null>", "to_action": "<zoom action>",
        "duration": 1.5, "reason": "…" }
    ],
    "onomatopoeia_changes": [
      { "type": "add"|"remove"|"move", "at_time": 14.5,
        "word": "BLAM", "animation": "explode_out", "intensity": 0.85, "reason": "…" }
    ],
    "subtitle_changes": []
  },
  "iterations_seen": [ { "iteration": 1, "score": 62, "verdict": "…", "summary": "…" } ]
}
```

**Iteration mechanics:** SimpleAutoSubs owns `iteration` and
`max_iterations`. Each cut, it writes the metadata extension above. The
strategist's task fires (hash changes when iteration changes), scores
the current cut, and writes its directive file. SimpleAutoSubs polls
the directive file:

- `verdict == "needs_edits"` AND `iteration < max_iterations`: apply
  `edit_directives`, write a new `shorts_metadata_<n>.json` with
  iteration+1, run cut, repeat.
- `verdict == "ship_*"` OR `iteration >= max_iterations`: ship the
  iteration named in `ship_which_iteration`. Stop the loop.

The strategist enforces a ship-anyway rule when:
- score >= 80 (good enough); OR
- projected improvement <= 5 points (no leverage left); OR
- iteration >= max_iterations (forced ship — picks best-scored seen).

**Strategist-side: shipped.** The task runs in advisory-only mode
when `editorial_decisions` is absent (pattern-level checklist) and
switches to full review mode automatically once it shows up.

**SimpleAutoSubs-side phase 1: shipped.** The metadata writer in
`core/video_processor.py` now emits the full `editorial_decisions`
block — trim segments (post-dialogue-extension), punch point + setup
rationale, the AI Director's zoom timeline, and the full onomatopoeia
event list — plus `file_info.iteration` (=1), `max_iterations` (=3),
and an empty `iteration_history`. `IntelligentTrimmer` stashes its
parsed JSON on `self.last_parsed_data` so the metadata writer can read
the punch_point fields without changing the trimmer's return type.

**SimpleAutoSubs-side phase 2: not shipped.** Required for the loop
to close end-to-end:
1. Add a directive consumer that reads
   `output/recommendations/edits/<base>.json` after each cut and
   parses its `edit_directives`.
2. On iteration > 1, skip the trim/director/onomatopoeia LLM phases
   and instead render from the prior iteration's
   `editorial_decisions` mutated by the directives. (Avoids the cost
   of re-running expensive LLM analysis when the input hasn't changed
   — only the editorial choices have.)
3. Add an iteration orchestrator that loops `process_single_video`
   up to `max_iterations` times, polling for fresh directives between
   iterations and writing per-iteration metadata
   (`shorts_metadata_<n>.json` updated each iteration with the new
   iteration number + `iteration_history` populated).
4. After iteration cap (or strategist `verdict=ship_*`), select the
   indicated iteration's output as the final ship file.

### Cross-project wiring (the unlock points)

Most of the strategist's product value is *advisory* until siblings
consume its output:

- **simpleautosubs reads `output/recommendations/titles/<base>.json`**
  before publish. This is what turns title recs from a hub UI tab into
  something that actually changes shipped titles. The title_provenance
  field in the existing `shorts_metadata_<n>.json` already has the
  shape; we just need simpleautosubs to check the strategist's verdict
  + recommended_title before finalizing. **Highest-leverage remaining
  cross-project change.**

- **simpleautosubs takes ownership of the capability manifest** and
  publishes it via an endpoint. Today the strategist holds the manifest
  as a fixture at `data/capability_manifest.json`. Once simpleautosubs
  owns it, `capability_gaps` swaps where it reads from and the manifest
  stays in sync with the code automatically.

- **publisher stamps `video_id` after upload** so the strategist can
  eventually reconstruct the cut → outcome join (the dead Pillar 2 from
  the original gameplan, see below).

- **simpleautosubs accepts re-edit directives** so `reedit_review`
  becomes actionable instead of advisory.

### Polish (the issues above, as concrete code changes)

- Promote `missing_patterns_flagged_by_critic` to first-class entries
  on the next `title_pattern_retro` run.
- Add `mean_alone < 0.05` warning on lever cards.
- Have the scorecard's critic prompt explicitly ask for a verdict on
  competing causal stories, not just a list of critiques.
- Add `MAX_TASKS_PER_SESSION` env var; surface session $ spent in the
  Thinker status header.

### Operational nice-to-haves

- Auto-start thinker on service launch (config flag).
- Open-trace-from-artifact link in the structured viewer.
- Per-section "view raw" so you can grab one slice without dumping the
  whole envelope.
- Truncate long monthly_trajectory in the JSON itself (or page it in
  the UI) — the data is fine but consumes payload size.

## On hold (the original Pillar 2)

The original gameplan described a SQLite store at `data/strategy.db`
joining simpleautosubs's edit decisions to shorts_analyzer's outcomes.
That table exists in `strategist/strategy.py` but is empty and unused —
it depends on `cut_id` flowing through simpleautosubs and the publisher,
which doesn't exist yet.

The thinker architecture sidesteps this by reading analyzer outputs
directly and treating pre-publish files (`shorts_metadata_<n>.json`) as
the substrate. If `cut_id` ever flows end-to-end, the strategy DB
becomes the natural way to join cut decisions to outcomes — and the
existing `record_cut` / `stamp_video_id` / `joined_for_channel`
functions wake up.

For now: don't build anything new on `strategy.py`. Either revive it
when cut_id ships, or delete it.

## Hard rules (still apply)

- **Never crash siblings.** Strategist calls degrade gracefully — if
  Gemini or Claude is unavailable the task records a skip reason rather
  than crashing. Same posture simpleautosubs takes with the analyzer.
- **No fallback titles.** If title generation fails, the title field is
  omitted; never invent placeholder text.
- **Two models, not one.** Gemini for generation, Claude for critique.
  If only one is available, the task should still work but record in the
  trace that the other was skipped.
- **Always write a trace.** Every multi-pass call writes a trace, even
  on failure.
- **Don't auto-trigger expensive analyzer reruns.** The strategist only
  consumes `/results/read` and `/videos`.
- **The `cut_id` is canonical** (once it exists). Don't introduce a
  competing identifier.

## What stays out of scope

- **Channel-level corpus reasoning** (`synthesis.json`, `tailwind.json`)
  is `shorts_analyzer`'s job. The strategist *consumes* and *critiques*
  those files; it doesn't replace the analyzer's per-video Gemini pass.
- **Cut execution** (FFmpeg, animations, subtitle embedding) is
  simpleautosubs's job. The strategist returns *directives*; the
  subtitler applies what it can.
- **Publishing** is `youtube_shorts_publisher`'s job.
- **Auto-running experiments.** The strategist *designs* experiments; it
  does not unilaterally route simpleautosubs traffic into arms.
- **Thumbnail generation.**

## Done when

The strategist is "complete enough to be load-bearing" when:

1. simpleautosubs reads the strategist's title recommendation before
   publish and either uses it or records why it didn't.
2. The capability_gaps artifact is read by a human (or by simpleautosubs's
   roadmap) often enough to drive feature work.
3. The channel scorecard's headline + actions are concrete enough that
   the operator changes editing/title behavior in response, and the
   recent-cohort median breakout responds.
4. The polish items above are addressed so the artifacts don't have
   visible footguns (multiplier inflation, dropped missing patterns,
   competing causal stories).

Anything beyond that is iteration on prompt quality and continued
expansion of the recommendation surface as the operator needs new
analytical lenses.
