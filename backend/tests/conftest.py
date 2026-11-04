from pathlib import Path
import shutil
import struct
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", max_upload_mb=2, max_video_seconds=5,
                    media_timeout_seconds=45, static_dir=tmp_path / "static")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg/FFprobe required for real media integration tests")
    root = tmp_path_factory.mktemp("media")
    paths = {name: root / f"{name}.mp4" for name in ("audio", "silent", "rotated", "vfr", "sar")}
    paths["mov"] = root / "source.mov"
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=1.5"]
    subprocess.run(base + ["-f", "lavfi", "-i", "sine=frequency=440:duration=1.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(paths["audio"])], check=True, timeout=20)
    subprocess.run(base + ["-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(paths["silent"])], check=True, timeout=20)
    # Write a real ISO BMFF display matrix. FFmpeg versions differ on whether
    # metadata rotate=90 creates one; a filename called 'rotated' is not proof.
    rotated = bytearray(paths["audio"].read_bytes())
    tkhd_type = rotated.index(b"tkhd")
    assert rotated[tkhd_type + 4] == 0  # Generated version-0 track header.
    offset = tkhd_type + 44
    rotated[offset:offset + 36] = struct.pack('>9i', 0, -65536, 0, 65536, 0, 0, 0, 0, 1073741824)
    paths["rotated"].write_bytes(rotated)
    subprocess.run(base + ["-vf", "select='not(mod(n,3))+not(mod(n,5))'", "-fps_mode", "vfr", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(paths["vfr"])], check=True, timeout=20)
    subprocess.run(base + ["-vf", "setsar=2/1", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(paths["sar"])], check=True, timeout=20)
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y","-i",str(paths["audio"]),"-c","copy","-f","mov",str(paths["mov"])],check=True,timeout=20)
    return paths
