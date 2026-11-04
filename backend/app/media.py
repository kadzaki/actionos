"""Bounded local media execution; model responses can never supply argv."""
from fractions import Fraction
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from .config import Settings
from .errors import AppError
from .models import Geometry, MediaMetadata
from .resources import MEDIA_SLOT, MediaBudget
from .timing import SourceTimeline
from .storage import atomic_replace


@lru_cache(maxsize=16)
def _tool_version(path,modified_ns,size):
    return run_media([path,'-version'],10).splitlines()[0]


def media_tool_versions():
    result={}
    for name in ('ffmpeg','ffprobe'):
        found=shutil.which(name)
        if not found:raise AppError('MEDIA_TOOLS_UNAVAILABLE','FFmpeg and FFprobe are required',503)
        try:info=Path(found).stat()
        except OSError:raise AppError('MEDIA_TOOLS_UNAVAILABLE','Media tools could not be inspected',503) from None
        result[name]=_tool_version(str(Path(found).resolve()),info.st_mtime_ns,info.st_size)
    return result


def run_media(arguments: list[str], timeout: int, *, budget: MediaBudget | None = None,
              max_stdout_bytes: int = 16 * 1024 * 1024) -> str:
    if Path(arguments[0]).stem.lower() in ('ffmpeg','ffprobe') and '-version' not in arguments:
        # Local media may not initiate network fetches through container references.
        arguments=[arguments[0],'-protocol_whitelist','file,pipe',*arguments[1:]]
    if not MEDIA_SLOT.acquire(timeout=min(timeout, 10)):
        raise AppError("MEDIA_BUSY", "Another media operation is using the processing slot; retry shortly", 503, True)
    process = None
    try:
        if budget:
            budget.check()
        # Bound memory and discard raw stderr so private paths/metadata never
        # become client errors or unbounded log buffers.
        with tempfile.TemporaryFile() as stdout:
            process = subprocess.Popen(arguments, shell=False, stdout=stdout,
                                       stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise AppError("MEDIA_TIMEOUT", "Media processing exceeded its runtime limit", 422, True)
                if os.fstat(stdout.fileno()).st_size > max_stdout_bytes:
                    raise AppError("MEDIA_OUTPUT_LIMIT", "Media metadata exceeded its output limit", 422)
                if budget:
                    budget.check()
                time.sleep(.05)
            if os.fstat(stdout.fileno()).st_size > max_stdout_bytes:
                raise AppError("MEDIA_OUTPUT_LIMIT", "Media metadata exceeded its output limit", 422)
            if budget:
                budget.check()
            if process.returncode != 0:
                raise AppError("MEDIA_PROCESS_FAILED", "Media could not be decoded or processed", 422)
            stdout.seek(0)
            return stdout.read(max_stdout_bytes).decode("utf-8", errors="replace")
    except FileNotFoundError:
        raise AppError("MEDIA_TOOL_MISSING", "FFmpeg/FFprobe is not installed", 503) from None
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        MEDIA_SLOT.release()


def media_tools():
    return {name: shutil.which(name) is not None for name in ("ffmpeg", "ffprobe")}


def probe(path: Path, settings: Settings, enforce_limits=True) -> MediaMetadata:
    raw = run_media(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], 20)
    try:
        data = json.loads(raw)
        stream = next(s for s in data["streams"] if s["codec_type"] == "video"
                      and not s.get("disposition", {}).get("attached_pic", 0))
        container = data["format"]["format_name"]
        if not any(c in container.split(",") for c in ("mov", "mp4")):
            raise ValueError("Invalid container")
        duration = float(stream.get("duration") or data["format"]["duration"])
        width, height = int(stream["width"]), int(stream["height"])
        fps = float(Fraction(stream.get("avg_frame_rate") or stream["r_frame_rate"]))
        if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0 or not 0 < fps <= 240:
            raise ValueError("Invalid metadata")
        ratio = stream.get("sample_aspect_ratio", "1:1")
        sar = float(Fraction(ratio.replace(":", "/"))) if ratio not in ("N/A", "0:1") else 1.0
        rotation = float(stream.get("tags", {}).get("rotate", 0))
        for side in stream.get("side_data_list", []):
            if "rotation" in side:
                rotation = float(side["rotation"])
        if not math.isfinite(rotation) or not 0 < sar <= 10:
            raise ValueError("Invalid geometry")
        rotation %= 360
        if min(abs(rotation - n) for n in (0, 90, 180, 270, 360)) > .1:
            raise AppError("UNSUPPORTED_ROTATION", "Use footage rotated by 0, 90, 180, or 270 degrees", 422)
        display_w, display_h = round(width * sar), height
        if round(rotation) in (90, 270):
            display_w, display_h = display_h, display_w
        duration_ms = round(duration * 1000)
        if enforce_limits and duration_ms > settings.max_video_seconds * 1000:
            raise AppError("VIDEO_TOO_LONG", f"Video must be at most {settings.max_video_seconds} seconds", 422)
        if width * height > settings.max_input_pixels or display_w * display_h > settings.max_input_pixels:
            raise AppError("VIDEO_TOO_LARGE", "Video resolution exceeds the configured processing limit", 422)
        audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
        return MediaMetadata(duration_ms=duration_ms, fps=fps, codec=stream["codec_name"],
                             container=container, audio_present=audio is not None,
                             audio_codec=audio.get("codec_name") if audio else None,
                             video_start_seconds=float(stream.get("start_time", 0)),
                             video_stream_index=int(stream["index"]), video_time_base=stream["time_base"],
                             geometry=Geometry(coded_width=width, coded_height=height,
                                               display_width=display_w, display_height=display_h,
                                               sample_aspect_ratio=sar, rotation_degrees=rotation))
    except AppError:
        raise
    except (ValueError, KeyError, StopIteration, TypeError, ZeroDivisionError, OverflowError):
        raise AppError("INVALID_VIDEO", "Upload a decodable MP4 or MOV with a valid video stream", 422) from None


def frame_timeline(path: Path, metadata: MediaMetadata, settings: Settings, budget: MediaBudget | None = None):
    raw = run_media(["ffprobe", "-v", "error", "-select_streams", str(metadata.video_stream_index), "-show_frames",
                     "-show_entries", "frame=best_effort_timestamp,best_effort_timestamp_time", "-of", "json", str(path)],
                    settings.media_timeout_seconds,budget=budget)
    try:
        pts = [int(f["best_effort_timestamp"]) for f in json.loads(raw)["frames"]]
        time_base = Fraction(metadata.video_time_base)
        seconds = [float(p*time_base) for p in pts]
        if not seconds or len(seconds) > settings.max_video_seconds * 240 + 4:
            raise ValueError("Invalid frame count")
        origin = seconds[0]
        times = [round((t - origin) * 1000, 4) for t in seconds]
        if times != sorted(times) or any(not math.isfinite(t) or t < 0 for t in times):
            raise ValueError("Invalid timestamps")
        return SourceTimeline.model_validate({"schema_version": 1, "origin_pts_seconds": origin,
                "source_time_convention": "milliseconds from first displayed video frame",
                "source_frame_times_ms": times,
                "working_fps": 30,
                "working_to_source": "Use working_frame_source_indices; elapsed clocks alone do not establish frame identity",
                "source_geometry": metadata.geometry.model_dump(),
                "source_frame_pts":pts,"source_time_base":metadata.video_time_base,
                "source_duration_ms":metadata.duration_ms,"video_stream_index":metadata.video_stream_index}).model_dump(mode="json")
    except (ValueError, KeyError, TypeError,ZeroDivisionError):
        raise AppError("INVALID_TIMELINE", "Could not establish a valid source frame timeline", 422) from None


def normalize(source: Path, output: Path, settings: Settings, preview=False,
              source_origin_seconds=0., duration_ms: int | None = None,
              source_metadata: MediaMetadata | None = None, budget: MediaBudget | None = None):
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.partial.mp4")
    scale = "scale=trunc(iw*sar/2)*2:trunc(ih/2)*2,setsar=1"
    if source_metadata:
        geometry = source_metadata.geometry
        width, height = geometry.display_width // 2 * 2, geometry.display_height // 2 * 2
        if min(width, height) < 2:
            raise AppError("INVALID_VIDEO", "Video display dimensions are too small", 422)
        scale = f"scale={width}:{height},setsar=1"
    if preview:
        bound_w=min(640,width) if source_metadata else 640
        bound_h=min(640,height) if source_metadata else 640
        scale += f",scale={bound_w}:{bound_h}:force_original_aspect_ratio=decrease:force_divisible_by=2"
    # Establish the output clock before scaling. Some FFmpeg builds discard
    # the final input frame duration through scale, shortening fps EOF output.
    video_filter=scale if preview else "setpts=PTS-STARTPTS,fps=30:round=near:eof_action=pass,"+scale
    try:
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-nostdin", "-y", "-copyts",
                   "-i", str(source), "-map", f"0:{source_metadata.video_stream_index}" if source_metadata else "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
                   "-vf", video_filter, "-af",
                   f"asetpts=PTS-{source_origin_seconds:.9f}/TB,aresample=async=1:first_pts=0",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "25" if preview else "18",
                   "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-threads", "2",
                   "-map_metadata", "-1", "-metadata:s:v:0", "rotate=0", "-movflags", "+faststart",
                   ]
        if preview:
            # The working source is already CFR and time-normalized. Preserve
            # every frame instead of running a second rounding/EOF conversion.
            args += ["-fps_mode","passthrough"]
        else:
            args += ["-fps_mode", "cfr", "-r", "30"]
            if duration_ms is not None:
                args += ["-t", f"{duration_ms/1000:.6f}"]
        args += [str(temporary)]
        run_media(args, settings.media_timeout_seconds, budget=budget)
        metadata = probe(temporary, settings, enforce_limits=False)
        atomic_replace(temporary, output)
        return metadata
    finally:
        temporary.unlink(missing_ok=True)


def thumbnail(source: Path, output: Path, settings: Settings,budget: MediaBudget | None = None):
    output.parent.mkdir(parents=True, exist_ok=True)
    run_media(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
               "-frames:v", "1", "-vf", "scale=480:-2", "-threads", "1", str(output)], 20,budget=budget)
