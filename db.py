from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS recordings (
                    id TEXT PRIMARY KEY,
                    source_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    record_status TEXT NOT NULL,
                    process_status TEXT NOT NULL DEFAULT 'pending',
                    process_stage TEXT NOT NULL DEFAULT '',
                    process_progress INTEGER NOT NULL DEFAULT 0,
                    output_dir TEXT NOT NULL,
                    file_path TEXT,
                    duration_seconds REAL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    stopped_at TEXT
                );

                CREATE TABLE IF NOT EXISTS clips (
                    id TEXT PRIMARY KEY,
                    recording_id TEXT NOT NULL REFERENCES recordings(id)
                        ON DELETE CASCADE,
                    clip_index INTEGER NOT NULL,
                    start_seconds REAL NOT NULL,
                    end_seconds REAL NOT NULL,
                    score REAL NOT NULL,
                    title TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    transcript TEXT NOT NULL DEFAULT '',
                    file_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(recording_id, clip_index)
                );

                CREATE INDEX IF NOT EXISTS idx_clips_recording
                    ON clips(recording_id, clip_index);
                """
            )

    def create_recording(
        self, recording_id: str, source_url: str, title: str, output_dir: str
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO recordings (
                    id, source_url, title, record_status, output_dir, created_at
                ) VALUES (?, ?, ?, 'starting', ?, ?)
                """,
                (recording_id, source_url, title, output_dir, utc_now()),
            )
        return self.get_recording(recording_id)

    def get_recording(self, recording_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recordings WHERE id = ?", (recording_id,)
            ).fetchone()
        if row is None:
            raise KeyError(recording_id)
        return dict(row)

    def list_recordings(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM recordings
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_recording(self, recording_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "title",
            "record_status",
            "process_status",
            "process_stage",
            "process_progress",
            "file_path",
            "duration_seconds",
            "error",
            "started_at",
            "stopped_at",
        }
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"Unsupported recording fields: {sorted(invalid)}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values()) + [recording_id]
        with self.connect() as connection:
            connection.execute(
                f"UPDATE recordings SET {assignments} WHERE id = ?", values
            )

    def replace_clips(
        self, recording_id: str, clips: list[dict[str, Any]]
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM clips WHERE recording_id = ?", (recording_id,)
            )
            for clip in clips:
                connection.execute(
                    """
                    INSERT INTO clips (
                        id, recording_id, clip_index, start_seconds, end_seconds,
                        score, title, reason, transcript, file_path, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clip["id"],
                        recording_id,
                        clip["clip_index"],
                        clip["start_seconds"],
                        clip["end_seconds"],
                        clip["score"],
                        clip["title"],
                        clip["reason"],
                        clip["transcript"],
                        clip["file_path"],
                        utc_now(),
                    ),
                )

    def list_clips(self, recording_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM clips WHERE recording_id = ?
                ORDER BY clip_index
                """,
                (recording_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_recording(self, recording_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM recordings WHERE id = ?", (recording_id,)
            )

    def inactive_recordings(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM recordings
                WHERE record_status IN ('recorded', 'failed')
                  AND process_status != 'processing'
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def recover_after_restart(self) -> None:
        """Mark jobs interrupted by an application restart as recoverable failures."""
        warning = "服务重启导致后台任务中断；录像分段和已生成切片仍保留在数据目录。"
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE recordings
                SET record_status = 'failed', stopped_at = ?, error = ?
                WHERE record_status IN ('starting', 'recording', 'stopping')
                """,
                (now, warning),
            )
            connection.execute(
                """
                UPDATE recordings
                SET process_status = 'failed', process_stage = 'interrupted', error = ?
                WHERE process_status = 'processing'
                """,
                (warning,),
            )

    def write_json(self, path: Path, value: Any) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )

