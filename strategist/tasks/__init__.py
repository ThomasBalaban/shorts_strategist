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
from . import experiment_evaluation
from . import effects_tuning

REGISTRY = [
    outcome_sync,          # free (no LLM) — keep the join table fresh first
    experiment_evaluation,  # scores ripe experiments (depends on outcome_sync)
    title_pattern_retro,
    pre_publish_title,
    pre_publish_metadata,
    channel_scorecard,
    capability_gaps,
    effects_tuning,       # Phase 6: fx directives from concluded experiments
    batch_experiment_assignment,
    pre_publish_edit_review,
]
