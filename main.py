from __future__ import annotations

import secrets
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .config import BASE_DIR, settings
from .db import Database
from .pipeline import ClippingPipeline
from .recorder import Recorder
from .utils import validate_douyin_url


settings.ensure_directories()
database = Database(settings.database_path)
database.initialize()
database.recover_after_restart()
recorder = Recorder(settings, database)
pipeline = ClippingPipeline(settings, database)

app = FastAPI(
    title="LiveClip",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
)
api = APIRouter(prefix="/api")
security = HTTPBasic(auto_error=False)


def require_admin(
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> str:
    if not settings.admin_password:
        raise HTTPException(
            status_code=503,
            detail="服务尚未设置 ADMIN_PASSWORD，请检查 .env",
        )
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="需要管理员登录",
            headers={"WWW-Authenticate": 'Basic realm="LiveClip"'},
        )
    valid_user = secrets.compare_digest(
        credentials.username.encode("utf-8"), settings.admin_user.encode("utf-8")
    )
    valid_password = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        settings.admin_password.encode("utf-8"),
    )
    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=401,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": 'Basic realm="LiveClip"'},
        )
    return credentials.username


class StartRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    title: str = Field(default="", max_length=80)

    @field_validator("url")
    @classmethod
    def check_url(cls, value: str) -> str:
        try:
            return validate_douyin_url(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class ProcessRequest(BaseModel):
    clip_count: int = Field(default=3, ge=1, le=10)
    min_seconds: float = Field(default=20.0, ge=10.0, le=60.0)
    max_seconds: float = Field(default=90.0, ge=30.0, le=180.0)

    @field_validator("max_seconds")
    @classmethod
    def check_duration(cls, value: float, info):
        minimum = info.data.get("min_seconds")
        if minimum is not None and value <= minimum:
            raise ValueError("最长时长必须大于最短时长")
        return value


def _record_payload(recording: dict[str, Any]) -> dict[str, Any]:
    payload = dict(recording)
    payload["video_url"] = (
        f"/api/recordings/{recording['id']}/video"
        if recording.get("file_path")
        else None
    )
    return payload


def _clip_payload(clip: dict[str, Any]) -> dict[str, Any]:
    payload = dict(clip)
    payload["preview_url"] = (
        f"/api/recordings/{clip['recording_id']}/clips/{clip['clip_index']}/preview"
    )
    payload["download_url"] = (
        f"/api/recordings/{clip['recording_id']}/clips/{clip['clip_index']}/download"
    )
    return payload


@app.get("/healthz", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok"}


def _storage_payload() -> dict[str, Any]:
    usage = shutil.disk_usage(settings.data_dir)
    total_gb = usage.total / (1024**3)
    used_gb = usage.used / (1024**3)
    free_gb = usage.free / (1024**3)
    return {
        "total_gb": round(total_gb, 2),
        "used_gb": round(used_gb, 2),
        "free_gb": round(free_gb, 2),
        "used_percent": round(used_gb / total_gb * 100, 1) if total_gb else 0,
        "min_start_free_gb": settings.min_free_gb,
        "min_residual_gb": settings.merge_reserve_gb,
        "can_start": free_gb >= settings.min_free_gb,
    }


@api.get("/storage")
def storage_status(_: str = Depends(require_admin)) -> dict[str, Any]:
    return _storage_payload()


@api.get("/runtime")
def runtime_status(_: str = Depends(require_admin)) -> dict[str, Any]:
    active_id = recorder.active_id
    active_recording = None
    if active_id:
        try:
            current = database.get_recording(active_id)
            active_recording = {
                "id": current["id"],
                "title": current["title"],
                "record_status": current["record_status"],
                "process_stage": current["process_stage"],
            }
        except KeyError:
            active_id = None
    return {
        "recording_busy": bool(active_id),
        "active_recording": active_recording,
        "clipping_busy": pipeline.busy,
        "storage": _storage_payload(),
    }


@api.get("/recordings")
def list_recordings(
    limit: int = Query(default=30, ge=1, le=100),
    _: str = Depends(require_admin),
) -> list[dict[str, Any]]:
    return [
        _record_payload(recording)
        for recording in database.list_recordings(limit=limit)
    ]


@api.get("/recordings/{recording_id}")
def get_recording(
    recording_id: str,
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    try:
        recording = database.get_recording(recording_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="录像任务不存在") from exc
    payload = _record_payload(recording)
    payload["clips"] = [
        _clip_payload(clip) for clip in database.list_clips(recording_id)
    ]
    return payload


@api.post("/recordings/start", status_code=202)
def start_recording(
    request: StartRequest,
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    try:
        recording = recorder.start(request.url, request.title)
    except (RuntimeError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _record_payload(recording)


@api.post("/recordings/{recording_id}/stop", status_code=202)
def stop_recording(
    recording_id: str,
    _: str = Depends(require_admin),
) -> dict[str, str]:
    try:
        recorder.stop(recording_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "stopping"}


@api.post("/recordings/{recording_id}/process", status_code=202)
def process_recording(
    recording_id: str,
    request: ProcessRequest,
    _: str = Depends(require_admin),
) -> dict[str, str]:
    try:
        recording = database.get_recording(recording_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="录像任务不存在") from exc
    if recording["record_status"] != "recorded":
        raise HTTPException(status_code=409, detail="录像尚未完整结束")
    try:
        pipeline.process_async(
            recording_id,
            request.clip_count,
            request.min_seconds,
            request.max_seconds,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "processing"}


def _assert_recording_deletable(recording: dict[str, Any]) -> None:
    if recorder.active_id == recording["id"]:
        raise HTTPException(
            status_code=409,
            detail="当前录像仍在运行，请先点击“停止录像”并等待合并完成。",
        )
    if recording["record_status"] in {"starting", "recording", "stopping"}:
        raise HTTPException(
            status_code=409,
            detail="该任务仍在录制或停止处理中，暂时不能删除。",
        )
    if recording["process_status"] == "processing":
        raise HTTPException(
            status_code=409,
            detail="该任务正在生成 AI 切片，暂时不能删除。",
        )


def _delete_recording_data(recording: dict[str, Any]) -> None:
    recordings_root = settings.recordings_dir.resolve()
    output_dir = Path(recording["output_dir"]).resolve()
    if output_dir == recordings_root or not output_dir.is_relative_to(recordings_root):
        raise RuntimeError("录像目录校验失败，已拒绝删除")
    if output_dir.exists():
        shutil.rmtree(output_dir)
    database.delete_recording(recording["id"])


@api.delete("/recordings/{recording_id}")
def delete_recording(
    recording_id: str,
    _: str = Depends(require_admin),
) -> dict[str, str]:
    try:
        recording = database.get_recording(recording_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="录像任务不存在") from exc
    _assert_recording_deletable(recording)
    try:
        _delete_recording_data(recording)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            status_code=500, detail=f"删除任务失败：{exc}"
        ) from exc
    return {"status": "deleted", "id": recording_id}


@api.post("/recordings/cleanup-history")
def cleanup_history(_: str = Depends(require_admin)) -> dict[str, Any]:
    deleted: list[str] = []
    failed: list[dict[str, str]] = []
    for recording in database.inactive_recordings():
        try:
            _assert_recording_deletable(recording)
            _delete_recording_data(recording)
            deleted.append(recording["id"])
        except Exception as exc:
            failed.append({"id": recording["id"], "error": str(exc)})
    return {"deleted_count": len(deleted), "deleted": deleted, "failed": failed}


@api.get("/recordings/{recording_id}/clips")
def list_clips(
    recording_id: str,
    _: str = Depends(require_admin),
) -> list[dict[str, Any]]:
    return [
        _clip_payload(clip) for clip in database.list_clips(recording_id)
    ]


@api.get("/recordings/{recording_id}/video")
def download_video(
    recording_id: str,
    _: str = Depends(require_admin),
):
    try:
        recording = database.get_recording(recording_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="录像任务不存在") from exc
    path = Path(recording["file_path"] or "")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="录像文件不存在")
    return FileResponse(
        path,
        media_type="video/x-matroska",
        filename=f"{recording['id']}.mkv",
    )


@api.get("/recordings/{recording_id}/clips/{clip_index}/download")
def download_clip(
    recording_id: str,
    clip_index: int,
    _: str = Depends(require_admin),
):
    clips = database.list_clips(recording_id)
    clip = next(
        (item for item in clips if item["clip_index"] == clip_index), None
    )
    if clip is None:
        raise HTTPException(status_code=404, detail="切片不存在")
    path = Path(clip["file_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="切片文件不存在")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"{recording_id}-clip-{clip_index:02d}.mp4",
    )


@api.get("/recordings/{recording_id}/clips/{clip_index}/preview")
def preview_clip(
    recording_id: str,
    clip_index: int,
    _: str = Depends(require_admin),
):
    clips = database.list_clips(recording_id)
    clip = next(
        (item for item in clips if item["clip_index"] == clip_index), None
    )
    if clip is None:
        raise HTTPException(status_code=404, detail="切片不存在")
    path = Path(clip["file_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="切片文件不存在")
    return FileResponse(path, media_type="video/mp4")


app.include_router(api)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")


@app.get("/", include_in_schema=False)
def index(_: str = Depends(require_admin)):
    return FileResponse(BASE_DIR / "app" / "static" / "index.html")

