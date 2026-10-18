"""HTTP byte-range responses for browser media seeking."""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse, Response, StreamingResponse


def ranged_file(path: Path, media_type: str, range_header: str | None = None) -> Response:
    """Serve a file or one requested byte range for media playback."""
    size = path.stat().st_size
    common_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }
    if not range_header or not range_header.startswith("bytes="):
        return FileResponse(path, media_type=media_type, headers=common_headers)

    spec = range_header.split("=", 1)[1].split(",", 1)[0].strip()
    start_text, separator, end_text = spec.partition("-")
    if not separator or not (start_text or end_text):
        return FileResponse(path, media_type=media_type, headers=common_headers)

    try:
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise ValueError
            start = max(0, size - suffix_length)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
    except ValueError:
        return FileResponse(path, media_type=media_type, headers=common_headers)

    if size == 0 or start < 0 or start >= size or end < start:
        return Response(
            status_code=416,
            headers={**common_headers, "Content-Range": f"bytes */{size}"},
        )

    end = min(end, size - 1)
    length = end - start + 1

    def iterator():
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iterator(), status_code=206, media_type=media_type,
        headers={
            **common_headers,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
        },
    )
