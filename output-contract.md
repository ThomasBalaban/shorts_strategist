# Output contract — finding the shipped video and title

The pre-publish iteration loop produces one **canonical metadata file
per video**. That file is the only thing a downstream consumer
(publisher, analyzer ingest, future Claude session, etc.) needs to read.

## Where to look

Per video, SimpleAutoSubs writes:

```
SimpleAutoSubs/shorts_data/shorts_metadata_<N>.json    ← one file per video
SimpleAutoSubs/output/<canonical>.mp4                  ← the shipped video file
```

`<N>` is auto-incremented; one metadata file per video. The metadata
file is a single-element JSON list: `[{ ... }]`.

## What to read from the metadata

```jsonc
[
  {
    "file_info": {
      "original_filename":  "Backtrack 2026-04-14 21-41-28.mkv",
      "output_filename":    "Backtrack 2026-04-14 21-41-28-as-9.mp4",
      "shipped_iteration":  2,
      "shipped_at":         "2026-05-04T14:23:11.041",
      "iteration":          2,
      "max_iterations":     3,
      "iteration_history":  [ /* prior iterations — files NOT on disk */ ]
    },
    "title":             "I died the exact second he said \"rest in peace\"",
    "title_analysis":    { /* clip_interpretation, patterns_applied, ... */ },
    "title_provenance":  { /* model, generated_at, channel_handle, ... */ },
    "editorial_decisions": { /* trim segments, zoom timeline, onomatopoeia */ }
  }
]
```

### To find the video file
Read `file_info.output_filename`. Look for that name in
`SimpleAutoSubs/output/`. Other paths referenced in
`iteration_history[*].output_filename` are **deleted on ship** —
they're a record of what was tried, not pointers to live files.

### To find the title
Read top-level `title`. The string is final and ship-ready (no escaping
or formatting needed). Provenance — model, channel, source synthesis,
trace_id — lives in `title_provenance`.

## Lifecycle guarantees

| Field | Set by | When |
|---|---|---|
| `file_info.output_filename` | iteration orchestrator | post-finalize, points at canonical file |
| `file_info.shipped_iteration` | iteration orchestrator | post-finalize only |
| `file_info.shipped_at` | iteration orchestrator | post-finalize only |
| `file_info.iteration` | initial cut, then orchestrator | every iteration |
| `title` / `title_analysis` / `title_provenance` | TitleGenerator | iteration 1 only (reused on subsequent iterations) |
| `editorial_decisions` | every iteration's pipeline | overwritten each iteration; final version reflects the shipped iteration |
| `iteration_history` | iteration orchestrator | grows as iterations complete |

If `shipped_iteration` is **absent**, the iteration loop wasn't run for
this video (legacy path). In that case `file_info.output_filename` is
still the right pointer — just no iteration history to inspect.

## Robustness rules for consumers

- **Always read `output_filename` from metadata, never derive from
  `original_filename`** — the orchestrator may rename across iterations.
- **Treat `iteration_history` as informational only** — never try to
  open files referenced there; they're deleted.
- **Don't assume `iteration == 1`** — many videos will ship at
  iteration 2 or 3.
- **Tolerate missing fields gracefully** — older metadata files
  (pre-iteration-loop) won't have `shipped_iteration` / `shipped_at` /
  `editorial_decisions` / `iteration_history`. Required fields are
  `file_info.original_filename`, `file_info.output_filename`, and
  (when present) `title`.
