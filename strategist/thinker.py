"""
Background thinker loop.

Drains a DAG of tasks one-per-tick. Each task hashes its inputs; if the hash
matches the artifact already on disk, the task is skipped. When every task is
either current or blocked on a missing dependency, the thinker enters
``idle`` and sleeps until inputs change or an operator forces a re-run.

State is held in-process; a small ``thinker_state.json`` file mirrors enough
across restarts that the UI shows continuity (last_tick_at, error log,
session call count).
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

import config
from . import inputs as inputs_mod
from . import recommendations
from .tasks import REGISTRY
from .tasks.base import DepKey, Task

# Tick cadence. Active = how often we look for stale work when busy. Idle =
# how long we sleep when the queue is empty. Both are short enough that the UI
# feels responsive.
ACTIVE_INTERVAL_SECONDS = 2.0
IDLE_INTERVAL_SECONDS = 30.0
ERROR_LOG_MAX = 50


@dataclass
class _State:
    state: str = "stopped"             # stopped | running | idle | error
    started_at: Optional[float] = None
    last_tick_at: Optional[float] = None
    last_drained_at: Optional[float] = None
    current_task: Optional[Dict[str, Any]] = None
    queue_depth: int = 0
    session_tasks_run: int = 0
    session_tasks_skipped: int = 0
    errors: List[Dict[str, Any]] = field(default_factory=list)
    last_snapshot_summary: Optional[Dict[str, Any]] = None
    forced_keys: List[List[str]] = field(default_factory=list)  # [[task_type, key], ...]


def _now() -> float:
    return time.time()


class Thinker:
    """Singleton background worker. Use :func:`get` to access."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state = _State()
        self._forced: Set[DepKey] = set()
        self._load_persisted_state()

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> Dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self._stop_event.clear()
            self._wake_event.clear()
            self._state = _State(state="running", started_at=_now(),
                                 errors=list(self._state.errors)[-ERROR_LOG_MAX:])
            self._thread = threading.Thread(target=self._run, name="thinker", daemon=True)
            self._thread.start()
            return self.status()

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                self._state.state = "stopped"
                return self.status()
            self._stop_event.set()
            self._wake_event.set()
        # Don't join under the lock; UI doesn't need to wait synchronously.
        return self.status()

    def force(self, task_type: str, key: Optional[str] = None) -> Dict[str, Any]:
        """Mark a specific task (or all tasks of a type) stale on next tick."""
        with self._lock:
            if key is None:
                self._forced.add((task_type, "*"))
            else:
                self._forced.add((task_type, key))
            self._state.forced_keys = [[t, k] for (t, k) in sorted(self._forced)]
            self._wake_event.set()
            return self.status()

    # ── status surface ────────────────────────────────────────────────────
    def status(self) -> Dict[str, Any]:
        with self._lock:
            s = self._state
            return {
                "state":               s.state,
                "started_at":          s.started_at,
                "last_tick_at":        s.last_tick_at,
                "last_drained_at":     s.last_drained_at,
                "current_task":        s.current_task,
                "queue_depth":         s.queue_depth,
                "session_tasks_run":   s.session_tasks_run,
                "session_tasks_skipped": s.session_tasks_skipped,
                "errors":              list(s.errors[-ERROR_LOG_MAX:]),
                "last_snapshot":       s.last_snapshot_summary,
                "forced":              list(s.forced_keys),
            }

    def queue_snapshot(self) -> List[Dict[str, Any]]:
        """Build a fresh queue without running anything — for UI inspection."""
        snap = inputs_mod.take()
        tasks = self._enumerate(snap)
        stale = self._filter_stale(tasks)
        ordered = self._topo_sort(stale, all_tasks=tasks)
        return [t.to_public() for t in ordered]

    # ── main loop ─────────────────────────────────────────────────────────
    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                with self._lock:
                    self._state.last_tick_at = _now()
                tick_did_work = self._tick()
                self._persist_state()
                interval = ACTIVE_INTERVAL_SECONDS if tick_did_work else IDLE_INTERVAL_SECONDS
                # Sleep but wake immediately on stop or force.
                self._wake_event.wait(timeout=interval)
                self._wake_event.clear()
        except Exception:
            self._record_error("thinker loop crashed", traceback.format_exc())
        finally:
            with self._lock:
                self._state.state = "stopped"
                self._state.current_task = None
            self._persist_state()

    def _tick(self) -> bool:
        snap = inputs_mod.take()
        with self._lock:
            self._state.last_snapshot_summary = snap.summary()

        tasks = self._enumerate(snap)
        forced_keys = self._consume_forced()
        stale = self._filter_stale(tasks, forced=forced_keys)
        ordered = self._topo_sort(stale, all_tasks=tasks)

        with self._lock:
            self._state.queue_depth = len(ordered)
            self._state.state = "running" if ordered else "idle"

        if not ordered:
            with self._lock:
                self._state.last_drained_at = _now()
                self._state.current_task = None
            return False

        task = ordered[0]
        with self._lock:
            self._state.current_task = task.to_public()
        try:
            task.run()
            with self._lock:
                self._state.session_tasks_run += 1
        except Exception:
            self._record_error(f"task {task.task_type}/{task.key} failed",
                               traceback.format_exc())
        finally:
            with self._lock:
                self._state.current_task = None
        return True

    # ── enumeration ───────────────────────────────────────────────────────
    def _enumerate(self, snap: inputs_mod.Snapshot) -> List[Task]:
        out: List[Task] = []
        for mod in REGISTRY:
            try:
                out.extend(mod.enumerate_tasks(snap))
            except Exception:
                self._record_error(f"enumerate {mod.__name__} failed",
                                   traceback.format_exc())
        return out

    def _filter_stale(self, tasks: List[Task],
                      forced: Optional[Set[DepKey]] = None) -> List[Task]:
        forced = forced or set()
        # "*" wildcard means every key of that task_type is forced.
        wildcards = {tt for (tt, k) in forced if k == "*"}
        stale: List[Task] = []
        skipped = 0
        for t in tasks:
            if t.task_type in wildcards or t.id in forced:
                stale.append(t)
                continue
            existing = recommendations.existing_input_hash(t.category, t.key)
            if existing != t.input_hash:
                stale.append(t)
            else:
                skipped += 1
        if skipped:
            with self._lock:
                self._state.session_tasks_skipped += skipped
        return stale

    def _topo_sort(self, stale: List[Task], all_tasks: List[Task]) -> List[Task]:
        """Return stale tasks in dependency-respecting order.

        A task may depend on tasks that aren't themselves stale; those deps
        are treated as already-satisfied.
        """
        stale_ids = {t.id for t in stale}
        by_id = {t.id: t for t in all_tasks}
        # Build adjacency restricted to stale tasks.
        in_deg: Dict[DepKey, int] = {t.id: 0 for t in stale}
        edges: Dict[DepKey, List[DepKey]] = {t.id: [] for t in stale}
        for t in stale:
            for dep in t.depends_on:
                if dep in stale_ids:
                    edges[dep].append(t.id)
                    in_deg[t.id] += 1
        # Kahn's algorithm.
        ready: Deque[DepKey] = deque(tid for tid, d in in_deg.items() if d == 0)
        ordered: List[Task] = []
        while ready:
            tid = ready.popleft()
            ordered.append(by_id[tid])
            for nxt in edges[tid]:
                in_deg[nxt] -= 1
                if in_deg[nxt] == 0:
                    ready.append(nxt)
        # If we missed any, there's a cycle — fall back to original order.
        if len(ordered) < len(stale):
            return stale
        return ordered

    def _consume_forced(self) -> Set[DepKey]:
        with self._lock:
            forced = set(self._forced)
            self._forced.clear()
            self._state.forced_keys = []
        return forced

    # ── error log + persistence ───────────────────────────────────────────
    def _record_error(self, msg: str, detail: str) -> None:
        entry = {"at": _now(), "msg": msg, "detail": detail.splitlines()[-5:]}
        with self._lock:
            self._state.errors.append(entry)
            self._state.errors = self._state.errors[-ERROR_LOG_MAX:]
            self._state.state = "error"

    def _persist_state(self) -> None:
        try:
            with self._lock:
                snap = {
                    "last_tick_at":          self._state.last_tick_at,
                    "last_drained_at":       self._state.last_drained_at,
                    "session_tasks_run":     self._state.session_tasks_run,
                    "session_tasks_skipped": self._state.session_tasks_skipped,
                    "errors":                self._state.errors[-ERROR_LOG_MAX:],
                }
            with open(config.THINKER_STATE_FILE, "w") as f:
                json.dump(snap, f, indent=2)
        except OSError:
            pass

    def _load_persisted_state(self) -> None:
        try:
            with open(config.THINKER_STATE_FILE) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        for k in ("last_tick_at", "last_drained_at",
                  "session_tasks_run", "session_tasks_skipped"):
            if k in data:
                setattr(self._state, k, data[k])
        if isinstance(data.get("errors"), list):
            self._state.errors = data["errors"][-ERROR_LOG_MAX:]


# ── module-level singleton ────────────────────────────────────────────────────
_INSTANCE: Optional[Thinker] = None


def get() -> Thinker:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = Thinker()
    return _INSTANCE
