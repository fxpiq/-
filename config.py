from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def _as_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _as_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("LIVECLIP_DATA_DIR", str(BASE_DIR / "data")))
    admin_user: str = os.getenv("ADMIN_USER", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    cookie_file: Path = Path(
        os.getenv("COOKIE_FILE", str(BASE_DIR / "secrets" / "cookies.txt"))
    )
    segment_seconds: int = _as_int("SEGMENT_SECONDS", 300)
    max_record_seconds: int = _as_int("MAX_RECORD_SECONDS", 4 * 60 * 60)
    min_free_gb: int = _as_int("MIN_FREE_GB", 20)
    merge_reserve_gb: int = _as_int("MERGE_RESERVE_GB", 8)
    keep_segments: bool = _as_bool("KEEP_SEGMENTS", False)
    whisper_model: str = os.getenv("WHISPER_MODEL", "small")
    whisper_device: str = os.getenv("WHISPER_DEVICE", "cpu")
    whisper_compute_type: str = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
    whisper_chunk_seconds: int = _as_int("WHISPER_CHUNK_SECONDS", 600)
    whisper_beam_size: int = _as_int("WHISPER_BEAM_SIZE", 3)
    llm_base_url: str = os.getenv("LLM_BASE_URL", "").strip()
    llm_model: str = os.getenv("LLM_MODEL", "").strip()
    llm_api_key: str = os.getenv("LLM_API_KEY", "").strip()

    @property
    def database_path(self) -> Path:
        return self.data_dir / "liveclip.db"

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_model and self.llm_api_key)

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()

