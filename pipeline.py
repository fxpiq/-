from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path
from typing import Callable

import httpx

from .config import Settings
from .db import Database, utc_now
from .highlights import (
    ClipCandidate,
    TranscriptSegment,
    build_candidates,
    merge_transcript_window,
    normalize_llm_selection,
)
from .subtitles import build_ass, escape_filter_path
from .transcriber import Transcriber
from .utils import get_media_duration, parse_json_from_text, run_command


class ClippingPipeline:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self.transcriber = Transcriber(settings)
        self._lock = threading.Lock()
        self._schedule_lock = threading.Lock()
        self._scheduled = False

    @property
    def busy(self) -> bool:
        with self._schedule_lock:
            return self._scheduled or self._lock.locked()

    def _update(
        self, recording_id: str, stage: str, progress: int, **fields
    ) -> None:
        self.database.update_recording(
            recording_id,
            process_stage=stage,
            process_progress=max(0, min(100, progress)),
            **fields,
        )

    def _llm_select(
        self,
        candidates: list[ClipCandidate],
        clip_count: int,
    ) -> list[ClipCandidate]:
        if not self.settings.llm_enabled or not candidates:
            return candidates[:clip_count]

        payload_candidates = []
        for item in candidates[:24]:
            payload_candidates.append(
                {
                    "candidate_id": item.candidate_id,
                    "start": item.start,
                    "end": item.end,
                    "duration": round(item.end - item.start, 2),
                    "local_score": item.score,
                    "transcript": item.transcript[:420],
                }
            )
        prompt = (
            "你是直播短视频编导。请从以下候选片段中选择最适合抖音传播的片段。"
            f"只选 {clip_count} 条，避免信息重复；优先选择有明确钩子、冲突、"
            "情绪变化、实用信息或完整包袱的内容。只返回 JSON，格式为 "
            '{"clips":[{"candidate_id":"c0001","title":"不超过16字",'
            '"reason":"不超过40字"}]}。不要编造候选 ID。\n\n'
            + json.dumps(payload_candidates, ensure_ascii=False)
        )
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {self.settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.settings.llm_model,
                "temperature": 0.2,
                "messages": [
                    {
                        "role": "system",
                        "content": "你只输出可解析的 JSON，不输出 Markdown。",
                    },
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=120,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = parse_json_from_text(content)
        return normalize_llm_selection(parsed, candidates, clip_count)

    def _fallback_candidates(
        self, duration: float, clip_count: int, min_seconds: float
    ) -> list[ClipCandidate]:
        if duration <= 0:
            return []
        length = min(max(min_seconds, duration / max(clip_count, 1)), 90.0)
        available = max(1, int(duration // length))
        count = min(clip_count, available)
        gap = max(0.0, (duration - count * length) / max(1, count + 1))
        results: list[ClipCandidate] = []
        for index in range(count):
            start = gap + index * (length + gap)
            end = min(duration, start + length)
            results.append(
                ClipCandidate(
                    candidate_id=f"f{index + 1:04d}",
                    start=round(start, 3),
                    end=round(end, 3),
                    score=1.0,
                    title=f"直播片段 {index + 1}",
                    reason="未识别到足够的语音，按时间均匀截取",
                    transcript="",
                )
            )
        return results

    def _render_clip(
        self,
        media_path: Path,
        output_path: Path,
        subtitle_path: Path,
        candidate: ClipCandidate,
    ) -> None:
        subtitle_filter = (
            f"ass='{escape_filter_path(subtitle_path)}'"
            if subtitle_path.stat().st_size > 400
            else "null"
        )
        filter_graph = (
            "[0:v]split=2[bgsrc][fgsrc];"
            "[bgsrc]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,gblur=sigma=24[bg];"
            "[fgsrc]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1,{subtitle_filter}[v]"
        )
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{candidate.start:.3f}",
            "-i",
            str(media_path),
            "-t",
            f"{candidate.end - candidate.start:.3f}",
            "-filter_complex",
            filter_graph,
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        result = run_command(command, timeout=1800)
        if result.returncode != 0 or not output_path.exists():
            raise RuntimeError(f"生成切片失败：{result.stderr[-1200:]}")

    def process(
        self,
        recording_id: str,
        clip_count: int = 3,
        min_seconds: float = 20.0,
        max_seconds: float = 90.0,
    ) -> list[dict]:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("已有切片任务正在运行")
        try:
            recording = self.database.get_recording(recording_id)
            if recording["record_status"] != "recorded":
                raise ValueError("录像尚未完整结束后，不能开始切片")
            media_path = Path(recording["file_path"] or "")
            if not media_path.exists():
                raise FileNotFoundError("录像文件不存在")

            output_dir = Path(recording["output_dir"])
            clips_dir = output_dir / "clips"
            work_dir = output_dir / "work"
            clips_dir.mkdir(parents=True, exist_ok=True)
            work_dir.mkdir(parents=True, exist_ok=True)
            transcript_path = work_dir / "transcript.json"

            self._update(
                recording_id,
                "starting",
                2,
                process_status="processing",
                error="",
            )
            segments = self.transcriber.transcribe(
                media_path,
                transcript_path,
                lambda stage, value: self._update(recording_id, stage, value),
            )
            self._update(recording_id, "正在分析高光", 72)

            candidates = build_candidates(
                segments,
                min_duration=min_seconds,
                max_duration=max_seconds,
                limit=36,
            )
            selected: list[ClipCandidate]
            if candidates:
                try:
                    selected = self._llm_select(candidates, clip_count)
                except Exception:
                    selected = candidates[:clip_count]
            else:
                duration = float(recording.get("duration_seconds") or 0)
                selected = self._fallback_candidates(
                    duration, clip_count, min_seconds
                )

            if not selected:
                raise RuntimeError("没有找到可生成的候选片段")

            self.database.write_json(
                work_dir / "candidates.json",
                [candidate.to_dict() for candidate in candidates],
            )
            rendered: list[dict] = []
            for index, candidate in enumerate(selected, start=1):
                stage = f"正在生成切片 {index}/{len(selected)}"
                progress = 75 + int(23 * (index - 1) / len(selected))
                self._update(recording_id, stage, progress)
                subtitle_path = work_dir / f"clip_{index:02d}.ass"
                build_ass(
                    segments,
                    candidate.start,
                    candidate.end,
                    subtitle_path,
                )
                output_path = clips_dir / f"clip_{index:02d}.mp4"
                self._render_clip(
                    media_path,
                    output_path,
                    subtitle_path,
                    candidate,
                )
                rendered.append(
                    {
                        "id": uuid.uuid4().hex,
                        "clip_index": index,
                        "start_seconds": candidate.start,
                        "end_seconds": candidate.end,
                        "score": candidate.score,
                        "title": candidate.title,
                        "reason": candidate.reason,
                        "transcript": merge_transcript_window(
                            segments, candidate.start, candidate.end
                        )
                        or candidate.transcript,
                        "file_path": str(output_path),
                    }
                )

            self.database.replace_clips(recording_id, rendered)
            self._update(
                recording_id,
                "切片完成",
                100,
                process_status="completed",
            )
            return self.database.list_clips(recording_id)
        except Exception as exc:
            try:
                self._update(
                    recording_id,
                    "处理失败",
                    0,
                    process_status="failed",
                    error=str(exc)[:2000],
                )
            except Exception:
                pass
            raise
        finally:
            self._lock.release()

    def process_async(
        self,
        recording_id: str,
        clip_count: int,
        min_seconds: float,
        max_seconds: float,
    ) -> threading.Thread:
        with self._schedule_lock:
            if self._scheduled or self._lock.locked():
                raise RuntimeError("已有切片任务正在运行")
            self._scheduled = True

        def run() -> None:
            try:
                self.process(
                    recording_id, clip_count, min_seconds, max_seconds
                )
            finally:
                with self._schedule_lock:
                    self._scheduled = False

        thread = threading.Thread(
            target=run,
            name=f"clip-{recording_id}",
            daemon=True,
        )
        thread.start()
        return thread

