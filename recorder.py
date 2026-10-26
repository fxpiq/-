from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .db import Database, utc_now
from .douyin_live import resolve_douyin_live
from .utils import (
    disk_free_gb,
    get_media_duration,
    run_command,
    safe_title,
    tail_text,
)


class Recorder:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self._state_lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._stop_events: dict[str, threading.Event] = {}
        self._active_id: str | None = None

    @property
    def active_id(self) -> str | None:
        with self._state_lock:
            return self._active_id

    def _is_alive(self, recording_id: str) -> bool:
        process = self._processes.get(recording_id)
        return process is not None and process.poll() is None

    def start(self, source_url: str, title: str) -> dict[str, Any]:
        with self._state_lock:
            if self._active_id and self._is_alive(self._active_id):
                raise RuntimeError("当前已有一路直播正在录制")
            free_gb = disk_free_gb(self.settings.data_dir)
            if free_gb < self.settings.min_free_gb:
                raise RuntimeError(
                    f"磁盘剩余空间仅 {free_gb:.1f} GB，低于 "
                    f"{self.settings.min_free_gb} GB 安全阈值"
                )
            recording_id = (
                datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            )
            output_dir = self.settings.recordings_dir / recording_id
            output_dir.mkdir(parents=True, exist_ok=False)
            recording = self.database.create_recording(
                recording_id,
                source_url,
                safe_title(title),
                str(output_dir),
            )
            self._active_id = recording_id
            self._stop_events[recording_id] = threading.Event()

        thread = threading.Thread(
            target=self._run,
            args=(recording_id, source_url),
            name=f"record-{recording_id}",
            daemon=True,
        )
        thread.start()
        return recording

    def stop(self, recording_id: str) -> None:
        with self._state_lock:
            if self._active_id != recording_id:
                raise ValueError("该任务不是当前正在录制的任务")
            event = self._stop_events.get(recording_id)
            process = self._processes.get(recording_id)
        if event:
            event.set()
        self.database.update_recording(
            recording_id, record_status="stopping", process_stage="正在安全停止"
        )
        if process and process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
            except ProcessLookupError:
                pass

    def _resolve_stream(self, source_url: str) -> dict[str, Any]:
        if (urlparse(source_url).hostname or "").lower() == "live.douyin.com":
            return resolve_douyin_live(source_url, self.settings.cookie_file)

        command = [
            "yt-dlp",
            "--no-warnings",
            "--no-playlist",
            "--socket-timeout",
            "30",
            "--dump-single-json",
        ]
        if self.settings.cookie_file.exists():
            command.extend(["--cookies", str(self.settings.cookie_file)])
        command.append(source_url)
        result = run_command(command, timeout=120)
        if result.returncode != 0:
            if "Unsupported URL" in result.stderr:
                try:
                    return resolve_douyin_live(
                        source_url, self.settings.cookie_file
                    )
                except RuntimeError as exc:
                    raise RuntimeError(
                        "yt-dlp 不支持该抖音直播链接，直播页回退解析也失败。"
                        f"\n{exc}"
                    ) from exc
            raise RuntimeError(
                "无法解析直播地址。请确认直播仍在进行，必要时配置 cookies.txt。"
                f"\n{result.stderr[-1200:]}"
            )
        try:
            info = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("直播解析结果不是有效 JSON") from exc

        live_status = info.get("live_status")
        if live_status and live_status not in {"is_live", "is_upcoming"}:
            raise RuntimeError(f"该地址当前不是直播状态：{live_status}")
        if info.get("is_live") is False:
            raise RuntimeError("该直播间当前未开播")

        stream_url = info.get("url")
        if not stream_url:
            for item in reversed(info.get("formats") or []):
                if item.get("url"):
                    stream_url = item["url"]
                    break
        if not stream_url:
            raise RuntimeError("未从直播间解析出可录制的媒体流")
        return {
            "url": stream_url,
            "headers": info.get("http_headers") or {},
            "resolved_title": info.get("title") or "",
        }

    def _run(self, recording_id: str, source_url: str) -> None:
        log_path = self.settings.recordings_dir / recording_id / "ffmpeg.log"
        output_dir = self.settings.recordings_dir / recording_id
        parts_dir = output_dir / "parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        process: subprocess.Popen[bytes] | None = None
        forced_warning = ""
        try:
            self.database.update_recording(
                recording_id, process_stage="正在解析直播间"
            )
            resolved = self._resolve_stream(source_url)
            title = self.database.get_recording(recording_id)["title"]
            resolved_title = (
                resolved.get("resolved_title") or resolved.get("title") or ""
            )
            if title == "直播录像" and resolved_title:
                self.database.update_recording(
                    recording_id, title=safe_title(resolved_title)
                )

            raw_headers = {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
                "Referer": "https://live.douyin.com/",
                **resolved["headers"],
            }
            safe_headers = {}
            for key, value in raw_headers.items():
                safe_key = re.sub(r"[^A-Za-z0-9-]", "", str(key))
                safe_value = str(value).replace("\r", "").replace("\n", " ")
                if safe_key and safe_value:
                    safe_headers[safe_key] = safe_value
            headers = "\r\n".join(
                f"{key}: {value}" for key, value in safe_headers.items()
            )
            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-y",
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                "15",
            ]
            if headers:
                command.extend(["-headers", headers + "\r\n"])
            command.extend(
                [
                    "-i",
                    resolved["url"],
                    "-t",
                    str(self.settings.max_record_seconds),
                    "-map",
                    "0:v?",
                    "-map",
                    "0:a?",
                    "-c",
                    "copy",
                    "-f",
                    "segment",
                    "-segment_format",
                    "mpegts",
                    "-segment_time",
                    str(self.settings.segment_seconds),
                    "-reset_timestamps",
                    "1",
                    str(parts_dir / "part_%05d.ts"),
                ]
            )
            with log_path.open("wb") as log_file:
                process = subprocess.Popen(
                    command,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                with self._state_lock:
                    self._processes[recording_id] = process
                if self._stop_events[recording_id].is_set():
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGINT)
                    except ProcessLookupError:
                        pass
                self.database.update_recording(
                    recording_id,
                    record_status="recording",
                    process_stage="录制中",
                    started_at=utc_now(),
                    error="",
                )
                last_disk_check = time.monotonic()
                while process.poll() is None:
                    if self._stop_events[recording_id].is_set():
                        break
                    if time.monotonic() - last_disk_check >= 30:
                        last_disk_check = time.monotonic()
                        recorded_bytes = sum(
                            part.stat().st_size
                            for part in parts_dir.glob("part_*.ts")
                        )
                        recorded_gb = recorded_bytes / (1024**3)
                        stop_threshold = max(
                            2.0,
                            recorded_gb + self.settings.merge_reserve_gb,
                        )
                        if disk_free_gb(self.settings.data_dir) < stop_threshold:
                            forced_warning = (
                                f"为给最终 MKV 合并预留空间，磁盘剩余低于 "
                                f"{stop_threshold:.1f} GB，系统已自动停止录像。"
                            )
                            try:
                                os.killpg(
                                    os.getpgid(process.pid), signal.SIGINT
                                )
                            except ProcessLookupError:
                                pass
                            break
                    time.sleep(0.5)
                if process.poll() is None:
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            try:
                                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            process.wait(timeout=5)

            parts = sorted(parts_dir.glob("part_*.ts"))
            if not parts:
                raise RuntimeError(
                    "录制流没有写入任何分段。请检查直播地址、cookies 和 ffmpeg.log。"
                )
            stopped_by_user = self._stop_events[recording_id].is_set()
            normal_exit = process is not None and process.returncode == 0
            self._finalize_recording(
                recording_id,
                output_dir,
                parts,
                log_path,
                warning=(
                    forced_warning
                    or (
                        ""
                        if stopped_by_user or normal_exit
                        else "录像进程异常结束，已保留结束前写入的内容。"
                    )
                ),
            )
        except Exception as exc:
            self.database.update_recording(
                recording_id,
                record_status="failed",
                process_stage="录制失败",
                stopped_at=utc_now(),
                error=str(exc)[:3000],
            )
        finally:
            with self._state_lock:
                if self._active_id == recording_id:
                    self._active_id = None
                self._processes.pop(recording_id, None)
                self._stop_events.pop(recording_id, None)
            if process and process.poll() is None:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def _finalize_recording(
        self,
        recording_id: str,
        output_dir: Path,
        parts: list[Path],
        log_path: Path,
        warning: str,
    ) -> None:
        recorded_bytes = sum(part.stat().st_size for part in parts)
        recorded_gb = recorded_bytes / (1024**3)
        required_free_gb = recorded_gb + self.settings.merge_reserve_gb
        current_free_gb = disk_free_gb(self.settings.data_dir)
        if current_free_gb < required_free_gb:
            raise RuntimeError(
                "磁盘空间不足，已停止合并以保护录像分段。"
                f"当前可用 {current_free_gb:.1f} GB，"
                f"合并需要至少 {required_free_gb:.1f} GB。"
                "请删除不需要的历史任务或扩容磁盘后重试。"
            )
        self.database.update_recording(
            recording_id,
            record_status="stopping",
            process_stage="正在合并录像",
        )
        concat_path = output_dir / "concat.txt"
        lines = []
        for part in parts:
            escaped = str(part.resolve()).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        final_path = output_dir / "recording.mkv"
        result = run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-map",
                "0",
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
                str(final_path),
            ],
            timeout=3600,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "录像分段已保留，但合并失败。可手动合并 parts 目录中的 .ts 文件。"
                f"\n{result.stderr[-1200:]}"
            )
        duration = get_media_duration(final_path)
        if duration <= 0:
            raise RuntimeError("合并后的录像无法读取时长")

        self.database.update_recording(
            recording_id,
            record_status="recorded",
            process_stage="录像完成",
            process_progress=0,
            file_path=str(final_path),
            duration_seconds=duration,
            stopped_at=utc_now(),
            error=warning,
        )
        if not self.settings.keep_segments:
            for part in parts:
                try:
                    part.unlink()
                except OSError:
                    pass
            try:
                concat_path.unlink()
            except OSError:
                pass

        if log_path.exists() and log_path.stat().st_size == 0:
            try:
                log_path.unlink()
            except OSError:
                pass

