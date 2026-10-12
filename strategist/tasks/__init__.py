"""
Thinker tasks.

Each task module exports an ``enumerate_tasks(snapshot) -> list[Task]`` plus a
Task dataclass with ``run()`` that writes its artifact via
``strategist.recommendations``. The thinker collects tasks from every
registered module, builds a DAG, and drains stale tasks one per tick.
"""
from . import title_pattern_retro
from . import pre_publish_title
from . import pre_publish_metadata
from . import channel_scorecard
from . import capability_gaps
from . import pre_publish_edit_review
from . import outcome_sync
from . import batch_experiment_assignment

REGISTRY = [
    outcome_sync,          # free (no LLM) — keep the join table fresh first
    title_pattern_retro,
    pre_publish_title,
    pre_publish_metadata,
    channel_scorecard,
    capability_gaps,
    batch_experiment_assignment,
    pre_publish_edit_review,
]
