from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

try:
    import fcntl
except ImportError:  # Windows development environments
    fcntl = None

from .config import Settings
from .highlights import TranscriptSegment
from .utils import get_media_duration


ProgressCallback = Callable[[str, int], None]

MODELSCOPE_REPOS = {
    "tiny": "pengzhendong/faster-whisper-tiny",
    "base": "pengzhendong/faster-whisper-base",
    "small": "pengzhendong/faster-whisper-small",
    "medium": "pengzhendong/faster-whisper-medium",
    "large-v3": "pengzhendong/faster-whisper-large-v3",
}


@contextmanager
def _model_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _download_modelscope_model(
    repo_id: str,
    destination: Path,
    progress: ProgressCallback,
) -> Path:
    api_url = (
        "https://modelscope.cn/api/v1/models/"
        + urllib.parse.quote(repo_id, safe="/")
        + "/repo/files?Revision=master&Recursive=true"
    )
    request = urllib.request.Request(
        api_url, headers={"User-Agent": "Mozilla/5.0 LiveClip/1.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    files = [
        item
        for item in payload.get("Data", {}).get("Files", [])
        if item.get("Type") == "blob" and item.get("Path")
    ]
    if not files:
        raise RuntimeError("国内模型镜像没有返回可下载文件")

    total_bytes = sum(int(item.get("Size") or 0) for item in files)
    completed_bytes = 0
    last_percent = -1
    destination.mkdir(parents=True, exist_ok=True)

    for item in files:
        relative_path = Path(*Path(item["Path"]).parts)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise RuntimeError("模型文件路径不合法")
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        expected_size = int(item.get("Size") or 0)
        if target.exists() and expected_size and target.stat().st_size == expected_size:
            completed_bytes += expected_size
            continue

        part_path = target.with_name(target.name + ".part")
        existing_part = part_path.stat().st_size if part_path.exists() else 0
        file_url = (
            "https://modelscope.cn/api/v1/models/"
            + urllib.parse.quote(repo_id, safe="/")
            + "/repo?"
            + urllib.parse.urlencode(
                {"Revision": "master", "FilePath": item["Path"]}
            )
        )
        last_error: Exception | None = None
        for attempt in range(1, 4):
            use_resume = expected_size >= 8 * 1024 * 1024
            if not use_resume and part_path.exists():
                part_path.unlink()
            existing_part = (
                part_path.stat().st_size
                if use_resume and part_path.exists()
                else 0
            )
            headers = {"User-Agent": "Mozilla/5.0 LiveClip/1.0"}
            if existing_part:
                headers["Range"] = f"bytes={existing_part}-"
            file_request = urllib.request.Request(file_url, headers=headers)
            file_progress_bytes = existing_part
            try:
                with urllib.request.urlopen(file_request, timeout=90) as response:
                    is_resume = response.status == 206 and existing_part > 0
                    mode = "ab" if is_resume else "wb"
                    if not is_resume:
                        file_progress_bytes = 0
                    with part_path.open(mode) as output:
                        while True:
                            # Some ModelScope file endpoints close early when a
                            # client requests a 1 MB block from small files.
                            chunk = response.read(64 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            file_progress_bytes += len(chunk)
                            overall = completed_bytes + file_progress_bytes
                            percent = (
                                int(overall * 100 / total_bytes)
                                if total_bytes
                                else 0
                            )
                            if percent != last_percent:
                                last_percent = percent
                                progress(
                                    f"正在从国内镜像下载语音模型 {percent}%",
                                    25 + int(percent * 13 / 100),
                                )
                actual_size = part_path.stat().st_size if part_path.exists() else 0
                if expected_size and actual_size != expected_size:
                    last_error = RuntimeError(
                        f"文件仅收到 {actual_size}/{expected_size} 字节"
                    )
                    if attempt < 3:
                        progress(
                            f"模型文件 {item['Path']} 不完整，正在重试 "
                            f"{attempt}/3",
                            25 + int(max(last_percent, 0) * 13 / 100),
                        )
                        time.sleep(attempt * 2)
                        continue
                    break
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    progress(
                        f"模型文件 {item['Path']} 下载中断，正在重试 "
                        f"{attempt}/3",
                        25 + int(last_percent * 13 / 100)
                        if last_percent >= 0
                        else 25,
                    )
                    time.sleep(attempt * 2)
        if last_error is not None:
            raise RuntimeError(
                f"下载语音模型失败（{item['Path']}）：{last_error}"
            ) from last_error

        if expected_size and part_path.stat().st_size != expected_size:
            raise RuntimeError(
                f"语音模型文件大小不完整：{item['Path']}"
            )
        expected_sha = item.get("Sha256")
        if expected_sha:
            digest = hashlib.sha256()
            with part_path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest().lower() != str(expected_sha).lower():
                raise RuntimeError(f"语音模型校验失败：{item['Path']}")
        os.replace(part_path, target)
        completed_bytes += expected_size

    progress("语音模型已就绪", 38)
    return destination


class Transcriber:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._model = None
        self._model_lock = threading.Lock()

    def _load_model(self, progress: ProgressCallback):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                model_reference = self.settings.whisper_model
                model_path = Path(model_reference)
                if model_path.is_dir():
                    model_reference = str(model_path)
                else:
                    repo_id = (
                        os.getenv("WHISPER_MODELSCOPE_ID", "").strip()
                        or MODELSCOPE_REPOS.get(self.settings.whisper_model, "")
                    )
                    if repo_id:
                        cache_root = Path(
                            os.getenv(
                                "MODEL_CACHE_DIR",
                                str(
                                    Path(os.getenv("HF_HOME", "/models"))
                                    / "modelscope"
                                ),
                            )
                        )
                        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "--", repo_id)
                        try:
                            with _model_lock(
                                cache_root / f"{safe_name}.lock"
                            ):
                                progress("正在检查语音模型缓存", 25)
                                model_path = _download_modelscope_model(
                                    repo_id,
                                    cache_root / safe_name,
                                    progress,
                                )
                            model_reference = str(model_path)
                        except Exception as exc:
                            progress(f"国内镜像不可用，尝试备用模型源：{exc}", 30)

                progress("正在加载语音模型", 39)
                self._model = WhisperModel(
                    model_reference,
                    device=self.settings.whisper_device,
                    compute_type=self.settings.whisper_compute_type,
                )
        return self._model

    def _extract_audio(
        self,
        media_path: Path,
        audio_dir: Path,
        progress: ProgressCallback,
    ) -> list[Path]:
        duration = get_media_duration(media_path)
        audio_dir.mkdir(parents=True, exist_ok=True)
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(media_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "segment",
            "-segment_time",
            str(self.settings.whisper_chunk_seconds),
            "-reset_timestamps",
            "1",
            "-segment_format",
            "wav",
            "-progress",
            "pipe:1",
            "-nostats",
            str(audio_dir / "chunk_%04d.wav"),
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        last_percent = -1
        assert process.stdout is not None
        for line in process.stdout:
            key, _, value = line.strip().partition("=")
            if key not in {"out_time_us", "out_time_ms"}:
                continue
            try:
                elapsed = int(value) / 1_000_000
            except ValueError:
                continue
            if duration > 0:
                ratio = min(1.0, elapsed / duration)
                percent = 8 + int(ratio * 12)
                if percent != last_percent:
                    last_percent = percent
                    progress(
                        f"正在提取音轨 {int(ratio * 100)}%",
                        percent,
                    )
        _, stderr = process.communicate()
        chunks = sorted(audio_dir.glob("chunk_*.wav"))
        if process.returncode != 0 or not chunks:
            raise RuntimeError(f"提取音轨失败：{stderr[-1000:]}")
        progress("音轨提取完成", 20)
        return chunks

    def transcribe(
        self,
        media_path: Path,
        cache_path: Path,
        progress: ProgressCallback,
    ) -> list[TranscriptSegment]:
        if cache_path.exists():
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                segments = [
                    TranscriptSegment(
                        start=float(item["start"]),
                        end=float(item["end"]),
                        text=str(item["text"]),
                    )
                    for item in payload["segments"]
                ]
                if segments:
                    progress("已读取字幕缓存", 68)
                    return segments
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                pass

        progress("正在提取音轨", 8)
        with tempfile.TemporaryDirectory(dir=cache_path.parent) as temp_dir:
            audio_dir = Path(temp_dir) / "audio_chunks"
            chunks = self._extract_audio(media_path, audio_dir, progress)

            progress("正在加载语音模型", 25)
            model = self._load_model(progress)
            progress("正在识别中文语音 0%", 40)
            segments: list[TranscriptSegment] = []
            total_duration = get_media_duration(media_path)
            chunk_offset = 0.0
            language = "zh"
            language_probability = None
            last_transcription_percent = -1
            for chunk_index, chunk_path in enumerate(chunks, start=1):
                progress(
                    f"正在识别中文语音：第 {chunk_index}/{len(chunks)} 段",
                    40 + int(
                        26
                        * min(1.0, chunk_offset / total_duration)
                        if total_duration > 0
                        else 0
                    ),
                )
                raw_segments, chunk_info = model.transcribe(
                    str(chunk_path),
                    language="zh",
                    beam_size=self.settings.whisper_beam_size,
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 450},
                    condition_on_previous_text=False,
                )
                if chunk_index == 1:
                    language = getattr(chunk_info, "language", "zh")
                    language_probability = getattr(
                        chunk_info, "language_probability", None
                    )
                for segment in raw_segments:
                    text = segment.text.strip()
                    absolute_start = chunk_offset + float(segment.start)
                    absolute_end = chunk_offset + float(segment.end)
                    if text:
                        segments.append(
                            TranscriptSegment(
                                start=absolute_start,
                                end=absolute_end,
                                text=text,
                            )
                        )
                    if total_duration > 0:
                        ratio = min(1.0, absolute_end / total_duration)
                        percent = 40 + int(ratio * 26)
                        if percent != last_transcription_percent:
                            last_transcription_percent = percent
                            progress(
                                f"正在识别中文语音 {int(ratio * 100)}%",
                                percent,
                            )
                chunk_offset += get_media_duration(chunk_path)

        payload = {
            "language": language,
            "language_probability": language_probability,
            "duration": total_duration,
            "segments": [
                {"start": item.start, "end": item.end, "text": item.text}
                for item in segments
            ],
        }
        cache_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        progress("语音识别完成", 68)
        return segments

