"""Checks for byte-range responses used by browser media seeking.

Run with: python -m tests.test_range_media
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.ranged import ranged_file

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        FAILURES.append(label)


async def response_body(response) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


def test_media_byte_ranges():
    path = Path(__file__).resolve().parent.parent / "LICENSE"
    content = path.read_bytes()
    response = ranged_file(path, "audio/wav", "bytes=5-9")
    body = asyncio.run(response_body(response))
    check("timestamp seeking can fetch only the requested media bytes",
          response.status_code == 206 and body == content[5:10]
          and response.headers.get("content-range") == f"bytes 5-9/{len(content)}")

    response = ranged_file(path, "audio/wav", "bytes=-4")
    body = asyncio.run(response_body(response))
    check("suffix ranges return the requested final bytes",
          response.status_code == 206 and body == content[-4:])

    response = ranged_file(path, "audio/wav", f"bytes={len(content) + 1}-")
    check("unsatisfiable seeks return a 416 range response",
          response.status_code == 416
          and response.headers.get("content-range") == f"bytes */{len(content)}")


def main() -> int:
    test_media_byte_ranges()
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
