"""Per-project disk ceilings and bounded local media-process concurrency."""
import os
from pathlib import Path
import shutil
from threading import BoundedSemaphore

from .config import Settings
from .errors import AppError

# Frame inspection and jobs share the same local media slot.
MEDIA_SLOT = BoundedSemaphore(1)


class MediaBudget:
    def __init__(self, project_root: Path, settings: Settings):
        self.root = project_root.resolve()
        self.limit_bytes = settings.max_project_mb * 1024 * 1024
        self.reserve_bytes = settings.min_free_disk_mb * 1024 * 1024

    def used_bytes(self):
        total = 0
        for directory, subdirs, files in os.walk(self.root, followlinks=False):
            for name in subdirs + files:
                path = Path(directory) / name
                if path.is_symlink():
                    raise AppError("INVALID_PATH", "Project storage contains an unsupported linked path", 409)
            for name in files:
                try:
                    total += (Path(directory) / name).stat().st_size
                except FileNotFoundError:
                    # A concurrent atomic replacement/cleanup can remove a temp file.
                    continue
        return total

    def check(self, additional_bytes=0):
        if self.used_bytes() + additional_bytes > self.limit_bytes:
            raise AppError("PROJECT_STORAGE_LIMIT", "Project reached its disk limit; remove unused projects or increase the configured limit", 507)
        if shutil.disk_usage(self.root).free - additional_bytes < self.reserve_bytes:
            raise AppError("DISK_SPACE_LOW", "Not enough free disk space to safely process media", 507, True)
