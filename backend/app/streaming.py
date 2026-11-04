"""Single byte-range streaming for browser seeking; never accepts a filesystem path from clients."""
import re
from pathlib import Path

from fastapi.responses import Response, StreamingResponse


class LeasedResponse(Response):
    """Release a read lease even if the client disconnects or send fails."""
    def __init__(self,response: Response,lease):
        self.response=response
        self.lease=lease
        super().__init__(status_code=response.status_code,headers=dict(response.headers))

    async def __call__(self,scope,receive,send):
        try:
            await self.response(scope,receive,send)
        finally:
            self.lease.__exit__(None,None,None)


def lease_response(store,project_id,builder):
    lease=store.media_lease(project_id)
    lease.__enter__()
    try:
        return LeasedResponse(builder(),lease)
    except BaseException:
        lease.__exit__(None,None,None)
        raise


def media_response(path: Path, range_header: str | None, head=False):
    size = path.stat().st_size
    start, end, status = 0, size - 1, 200
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-cache",
               "X-Content-Type-Options": "nosniff"}
    if range_header:
        if len(range_header) > 100:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
        if not match or not any(match.groups()):
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        left, right = match.groups()
        if not left:
            length = int(right)
            start = max(0, size - length)
            if not length:
                start = size
        else:
            start = int(left)
            end = min(int(right), end) if right else end
        if start >= size or end < start:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(max(0, end - start + 1))
    if head:
        return Response(status_code=status, headers=headers, media_type="video/mp4")

    def chunks():
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = stream.read(min(remaining, 256 * 1024))
                if not chunk:
                    return
                remaining -= len(chunk)
                yield chunk
    return StreamingResponse(chunks(), status_code=status, headers=headers, media_type="video/mp4")
