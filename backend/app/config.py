from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlsplit
from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data")
    static_dir: Path = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    model_api_key: str = field(default="", repr=False)
    meta_api_base_url: str = "https://api.meta.ai/v1"
    muse_model: str = "muse-spark-1.3"
    sam_model: str = "sam-3.1"
    max_upload_mb: int = 150
    max_video_seconds: int = 120
    max_concurrent_jobs: int = 1
    max_queued_jobs: int = 8
    media_timeout_seconds: int = 300
    max_input_pixels: int = 8_294_400
    app_port: int = 8000
    max_project_mb: int = 2048
    min_free_disk_mb: int = 256
    max_cached_frames: int = 600

    def __post_init__(self):
        for name in ("max_upload_mb", "max_video_seconds", "max_queued_jobs",
                     "media_timeout_seconds", "max_input_pixels", "max_project_mb",
                     "min_free_disk_mb", "max_cached_frames"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_concurrent_jobs != 1:
            raise ValueError("VideoCut requires MAX_CONCURRENT_JOBS=1 and one Uvicorn worker")
        if not 1 <= self.app_port <= 65535:
            raise ValueError("APP_PORT must be between 1 and 65535")
        parsed = urlsplit(self.meta_api_base_url)
        if (parsed.scheme != "https" or parsed.hostname != "api.meta.ai"
                or parsed.path.rstrip("/") != "/v1" or parsed.query or parsed.fragment
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise ValueError("Use the official Meta cloud API base https://api.meta.ai/v1")

    @classmethod
    def from_env(cls,env_file: Path | None = None):
        path=env_file if env_file is not None else Path(__file__).resolve().parents[2]/'.env'
        environment={**(dotenv_values(path,interpolate=False) if path.is_file() else {}),**os.environ}
        values = {}
        for name in cls.__dataclass_fields__:
            raw = environment.get(name.upper())
            if raw is not None:
                if name.endswith("_dir"):
                    values[name] = Path(raw)
                elif name.startswith("max_") or name in ("media_timeout_seconds", "app_port", "min_free_disk_mb"):
                    values[name] = int(raw)
                else:
                    values[name] = raw
        return cls(**values)

    @property
    def max_upload_bytes(self):
        return self.max_upload_mb * 1024 * 1024
