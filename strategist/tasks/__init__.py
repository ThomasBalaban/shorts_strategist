"""
Thinker tasks.

Each task module exports an ``enumerate_tasks(snapshot) -> list[Task]`` plus a
Task dataclass with ``run()`` that writes its artifact via
``strategist.recommendations``. The thinker collects tasks from every
registered module, builds a DAG, and drains stale tasks one per tick.
"""
from . import title_pattern_retro
from . import pre_publish_title
from . import channel_scorecard
from . import capability_gaps

REGISTRY = [
    title_pattern_retro,
    pre_publish_title,
    channel_scorecard,
    capability_gaps,
]
