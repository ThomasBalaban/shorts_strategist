import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS cuts (
    cut_id          TEXT PRIMARY KEY,
    channel_handle  TEXT NOT NULL,
    video_id        TEXT,
    created_at      REAL NOT NULL,
    edit_decisions  TEXT NOT NULL,
    title           TEXT,
    title_reasoning TEXT
);
CREATE INDEX IF NOT EXISTS ix_cuts_channel ON cuts(channel_handle);
CREATE INDEX IF NOT EXISTS ix_cuts_video ON cuts(video_id);

CREATE TABLE IF NOT EXISTS outcomes (
    video_id        TEXT PRIMARY KEY,
    channel_handle  TEXT NOT NULL,
    captured_at     REAL NOT NULL,
    breakout_score  REAL,
    retention_curve TEXT,
    raw             TEXT
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id   TEXT PRIMARY KEY,
    channel_handle  TEXT NOT NULL,
    created_at      REAL NOT NULL,
    hypothesis      TEXT NOT NULL,
    arms            TEXT NOT NULL,
    success_metric  TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'designed',
    conclusion      TEXT
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(config.STRATEGY_DB)
    c.row_factory = sqlite3.Row
    return c


def init_schema() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)


def record_cut(cut_id: str, channel_handle: str, edit_decisions: Dict[str, Any],
               title: Optional[str] = None, title_reasoning: Optional[str] = None,
               video_id: Optional[str] = None) -> None:
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO cuts
               (cut_id, channel_handle, video_id, created_at,
                edit_decisions, title, title_reasoning)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cut_id, channel_handle, video_id, time.time(),
             json.dumps(edit_decisions), title, title_reasoning),
        )


def stamp_video_id(cut_id: str, video_id: str) -> None:
    with _conn() as c:
        c.execute("UPDATE cuts SET video_id = ? WHERE cut_id = ?", (video_id, cut_id))


def record_outcome(video_id: str, channel_handle: str, breakout_score: Optional[float],
                   retention_curve: Optional[List[float]], raw: Dict[str, Any]) -> None:
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO outcomes
               (video_id, channel_handle, captured_at, breakout_score,
                retention_curve, raw)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (video_id, channel_handle, time.time(), breakout_score,
             json.dumps(retention_curve) if retention_curve is not None else None,
             json.dumps(raw)),
        )


def joined_for_channel(channel_handle: str) -> List[Dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            """SELECT c.cut_id, c.video_id, c.edit_decisions, c.title,
                      o.breakout_score, o.retention_curve
               FROM cuts c
               LEFT JOIN outcomes o ON o.video_id = c.video_id
               WHERE c.channel_handle = ?
               ORDER BY c.created_at DESC""",
            (channel_handle,),
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "cut_id": r["cut_id"],
            "video_id": r["video_id"],
            "edit_decisions": json.loads(r["edit_decisions"]),
            "title": r["title"],
            "breakout_score": r["breakout_score"],
            "retention_curve": json.loads(r["retention_curve"]) if r["retention_curve"] else None,
        })
    return out


def save_experiment(experiment_id: str, channel_handle: str, hypothesis: str,
                    arms: List[Dict[str, Any]], success_metric: str) -> None:
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO experiments
               (experiment_id, channel_handle, created_at, hypothesis,
                arms, success_metric, status)
               VALUES (?, ?, ?, ?, ?, ?, 'designed')""",
            (experiment_id, channel_handle, time.time(), hypothesis,
             json.dumps(arms), success_metric),
        )


def list_experiments(channel_handle: Optional[str] = None) -> List[Dict[str, Any]]:
    with _conn() as c:
        if channel_handle:
            rows = c.execute(
                "SELECT * FROM experiments WHERE channel_handle = ? ORDER BY created_at DESC",
                (channel_handle,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM experiments ORDER BY created_at DESC"
            ).fetchall()
    out = []
    for r in rows:
        out.append({
            "experiment_id": r["experiment_id"],
            "channel_handle": r["channel_handle"],
            "hypothesis": r["hypothesis"],
            "arms": json.loads(r["arms"]),
            "success_metric": r["success_metric"],
            "status": r["status"],
            "conclusion": r["conclusion"],
        })
    return out
