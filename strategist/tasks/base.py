"""Task base type and shared helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Tuple


# A dependency is identified by (task_type, key) — same shape as a Task's id.
DepKey = Tuple[str, str]


@dataclass
class Task:
    task_type: str                 # e.g. "postmortem", "pre_publish_title"
    key: str                       # filename stem under the artifact dir
    category: str                  # recommendations.CATEGORIES key
    input_hash: str                # hash of the inputs this task consumes
    depends_on: List[DepKey] = field(default_factory=list)
    # ``run`` is set by the enumerate() function so the closure can capture
    # whichever snapshot/inputs it needs. The thinker just calls it.
    run: Callable[[], Dict[str, Any]] = field(default=lambda: {})
    description: str = ""           # short human label for UI

    @property
    def id(self) -> DepKey:
        return (self.task_type, self.key)

    def to_public(self) -> Dict[str, Any]:
        """JSON-safe representation for the queue UI."""
        return {
            "task_type":   self.task_type,
            "key":         self.key,
            "category":    self.category,
            "input_hash":  self.input_hash,
            "depends_on":  [list(d) for d in self.depends_on],
            "description": self.description,
        }
