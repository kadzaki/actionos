"""Run-ready source evidence adapter, using actual PTS and verified derivatives."""
from bisect import bisect_right
from math import isfinite
from uuid import UUID

from pydantic import Field

from .errors import AppError
from .evidence import evidence_frame, read_timeline
from .models import Asset, Model
from .programs.policy import SourceInterval
from .storage import file_sha256
from .timing import CoordinateTransform, scale_transform


class SourceEvidenceReference(Model):
    project_id: UUID
    source_asset_id: UUID
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    evidence_frame_id: UUID
    source_frame_index: int = Field(ge=0)
    source_time_ms: float = Field(ge=0)
    observed_at_source_ms: float | None = Field(default=None,ge=0,strict=True)
    source_pts: int
    source_time_base: str
    image_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    coordinate_space: str = 'normalized_source_display'
    transforms: list[CoordinateTransform]

    @property
    def source_url(self):
        time_ms=self.source_time_ms if self.observed_at_source_ms is None else self.observed_at_source_ms
        return f'/api/assets/{self.source_asset_id}/stream?variant=original#t={time_ms / 1000:.6f}'

    @property
    def image_url(self):
        return f'/api/evidence/{self.evidence_frame_id}/image'


class SourceEvidenceAdapter:
    def __init__(self, store, settings, project_id, asset_id):
        self.store, self.settings = store, settings
        self.asset = store.load(project_id, 'assets', Asset, asset_id)
        asset = self.asset
        if asset.status != 'ready' or not asset.metadata:
            raise AppError('MEDIA_NOT_READY', 'Source evidence requires prepared media', 409)
        self._verify(asset.source_path, asset.sha256)
        required = (asset.working_path, asset.proxy_path, asset.thumbnail_path, asset.timeline_path)
        if not all(required) or any(path not in asset.artifact_sha256 for path in required):
            raise AppError('MEDIA_INTEGRITY_UNQUALIFIED', 'Legacy derivatives need integrity requalification before a program run', 409)
        for path in required:
            self._verify(path, asset.artifact_sha256[path])
        self.timeline = read_timeline(store, asset, settings)
        if self.timeline.source_duration_ms != asset.metadata.duration_ms:
            raise AppError('SOURCE_MISMATCH', 'Timeline duration differs from its source', 409)

    def _verify(self, relative, expected):
        path = self.store.path(self.asset.project_id, relative)
        try:
            if not path.is_file() or file_sha256(path) != expected:
                raise AppError('CORRUPT_ARTIFACT', 'Source or derivative integrity check failed', 409)
        except OSError:
            raise AppError('CORRUPT_ARTIFACT', 'Source or derivative could not be read', 409) from None

    def interval(self, start_ms, end_ms):
        interval = SourceInterval(start_ms=start_ms, end_ms=end_ms)
        if interval.end_ms > self.asset.metadata.duration_ms:
            raise AppError('INVALID_INTERVAL', 'Evidence interval is outside the original source', 422)
        return interval

    def frame(self, requested_ms, *, source_frame_index=None):
        # Recheck immutable source before extraction or returning cached evidence.
        self._verify(self.asset.source_path, self.asset.sha256)
        self._verify(self.asset.timeline_path, self.asset.artifact_sha256[self.asset.timeline_path])
        frame = evidence_frame(self.store, self.settings, self.asset, requested_ms,
                               source_frame_index=source_frame_index)
        index=frame.source_frame_index
        if (frame.source_time_ms!=self.timeline.source_frame_times_ms[index]
                or frame.source_pts!=self.timeline.source_frame_pts[index]
                or frame.source_time_base!=self.timeline.source_time_base):
            raise AppError('EVIDENCE_MISMATCH','Cached frame metadata differs from the exact original PTS timeline',409)
        g = self.asset.metadata.geometry
        transforms = [*self.timeline.transforms,
                      scale_transform('source_display_pixels', 'normalized_source_display',
                                      (g.display_width, g.display_height), (1, 1))]
        return SourceEvidenceReference(project_id=frame.project_id, source_asset_id=frame.source_asset_id,
            source_sha256=frame.source_sha256, evidence_frame_id=frame.id,
            source_frame_index=frame.source_frame_index, source_time_ms=frame.source_time_ms,
            source_pts=frame.source_pts, source_time_base=frame.source_time_base,
            image_sha256=frame.image_sha256, transforms=transforms)

    def working_frame(self, time_ms):
        if (not self.asset.working_metadata or not isfinite(time_ms)
                or not 0 <= time_ms < self.asset.working_metadata.duration_ms):
            raise AppError('INVALID_TIMESTAMP', 'Working timestamp is outside prepared media', 422)
        index = max(0, bisect_right(self.timeline.working_frame_times_ms, time_ms) - 1)
        source_index = self.timeline.working_frame_source_indices[index]
        requested = min(int(self.timeline.source_frame_times_ms[source_index]), self.asset.metadata.duration_ms - 1)
        return self.frame(requested, source_frame_index=source_index)

    def window_frame(self, window, local_frame_index):
        if (window.project_id != self.asset.project_id or window.source_asset_id != self.asset.id
                or window.source_sha256 != self.asset.sha256):
            raise AppError('OWNERSHIP', 'Provider window is not bound to this source', 409)
        self._verify(window.path, window.sha256)
        if type(local_frame_index) is not int or not 0 <= local_frame_index < len(window.source_frame_indices):
            raise AppError('INVALID_FRAME', 'Provider frame index is outside its input window', 422)
        source_index = window.source_frame_indices[local_frame_index]
        if (not 0 <= source_index < len(self.timeline.source_frame_times_ms)
                or window.source_frame_times_ms[local_frame_index] != self.timeline.source_frame_times_ms[source_index]):
            raise AppError('SOURCE_MISMATCH', 'Provider window lost original frame identity', 409)
        requested = min(int(window.source_frame_times_ms[local_frame_index]), self.asset.metadata.duration_ms - 1)
        return self.frame(requested, source_frame_index=source_index)

    def validate_witness(self,source_frame_index,observed_at_source_ms):
        times=self.timeline.source_frame_times_ms
        if type(source_frame_index) is not int or not 0<=source_frame_index<len(times):
            raise AppError('INVALID_FRAME','Evidence references an invalid original frame',422)
        if observed_at_source_ms is None:return
        if type(observed_at_source_ms) not in (int,float) or not isfinite(observed_at_source_ms):
            raise AppError('INVALID_TIMESTAMP','Observed frame time must be finite source milliseconds',422)
        stop=bisect_right(times,times[source_frame_index])
        if source_frame_index!=stop-1:
            raise AppError('AMBIGUOUS_FRAME_TIME','An earlier duplicate PTS frame has no unique displayed interval',422)
        end=times[stop] if stop<len(times) else self.asset.metadata.duration_ms
        if not times[source_frame_index]<=observed_at_source_ms<end:
            raise AppError('EVIDENCE_TIME_MISMATCH','Observation time is outside the original frame\'s actual display interval',422)

    def frame_witness(self,source_frame_index,observed_at_source_ms):
        """A held VFR frame keeps its PTS/ID while witnessed later in its display interval."""
        self.validate_witness(source_frame_index,observed_at_source_ms)
        requested=min(int(self.timeline.source_frame_times_ms[source_frame_index]),self.asset.metadata.duration_ms-1)
        frame=self.frame(requested,source_frame_index=source_frame_index)
        return frame.model_copy(update={'observed_at_source_ms':float(observed_at_source_ms)})

    @staticmethod
    def normalized_box(window, x1, y1, x2, y2):
        # SAM inclusive pixel corners -> continuous normalized display edges.
        if (any(type(v) is not int for v in (x1, y1, x2, y2))
                or not 0 <= x1 <= x2 < window.width or not 0 <= y1 <= y2 < window.height):
            raise AppError('INVALID_GEOMETRY', 'Box is outside its declared provider image', 422)
        return (x1 / window.width, y1 / window.height,
                (x2 - x1 + 1) / window.width, (y2 - y1 + 1) / window.height)
