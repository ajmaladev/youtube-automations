"""Checks a rendered MP4 before it is staged for review, so a broken render is retried instead of posted.

Stdlib only (no ffprobe): walks the MP4 box tree and requires a complete moov box, a video track with the
expected frame size, an audio track (the narration) and a duration that fits the script's word count.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterator

MIN_BYTES = 300_000
FRAME_SIZES = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}
SECONDS_PER_WORD = (0.2, 0.8)  # narration pace bounds; the TTS voice reads about 0.35 s per word
MIN_SECONDS, MAX_SECONDS = 5.0, 900.0


class VideoCheckError(RuntimeError):
    pass


@dataclass
class Mp4Info:
    duration_s: float = 0.0
    tracks: list[dict[str, Any]] = field(default_factory=list)  # {"handler": "vide"|"soun", "width", "height"}

    @property
    def frame_size(self) -> tuple[int, int]:
        video = next((t for t in self.tracks if t["handler"] == "vide"), None)
        return (video["width"], video["height"]) if video else (0, 0)


def _boxes(f: BinaryIO, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """(type, body start, box end) for each box between start and end."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        size, kind = struct.unpack(">I4s", f.read(8))
        header = 8
        if size == 1:
            size, header = struct.unpack(">Q", f.read(8))[0], 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise VideoCheckError(f"corrupt MP4: box {kind!r} at byte {pos} runs past the end of the file")
        yield kind, pos + header, pos + size
        pos += size


def _track(f: BinaryIO, start: int, end: int) -> dict[str, Any]:
    track: dict[str, Any] = {"handler": "", "width": 0, "height": 0}
    for kind, body, box_end in _boxes(f, start, end):
        if kind == b"tkhd":
            f.seek(body)
            version = f.read(1)[0]
            f.seek(body + 4 + (32 if version == 1 else 20) + 52)  # skip times/ids, then layer..matrix
            width, height = struct.unpack(">II", f.read(8))
            track["width"], track["height"] = width >> 16, height >> 16  # 16.16 fixed point
        elif kind == b"mdia":
            for sub, sub_body, _ in _boxes(f, body, box_end):
                if sub == b"hdlr":
                    f.seek(sub_body + 8)
                    track["handler"] = f.read(4).decode("latin-1")
    return track


def read_mp4(path: Path) -> Mp4Info:
    info, size = Mp4Info(), Path(path).stat().st_size
    with open(path, "rb") as f:
        moov = next(((body, end) for kind, body, end in _boxes(f, 0, size) if kind == b"moov"), None)
        if moov is None:
            raise VideoCheckError("no moov box: the file is incomplete")
        for kind, body, end in _boxes(f, *moov):
            if kind == b"mvhd":
                f.seek(body)
                version = f.read(1)[0]
                f.seek(body + 4 + (16 if version == 1 else 8))
                fmt = ">IQ" if version == 1 else ">II"
                timescale, duration = struct.unpack(fmt, f.read(struct.calcsize(fmt)))
                info.duration_s = duration / timescale if timescale else 0.0
            elif kind == b"trak":
                info.tracks.append(_track(f, body, end))
    return info


def check_video(path: Path, words: int = 0, aspect: str = "9:16") -> Mp4Info:
    path = Path(path)
    size = path.stat().st_size if path.is_file() else 0
    if size < MIN_BYTES:
        raise VideoCheckError(f"{path.name} is missing or too small ({size} bytes)")
    try:
        info = read_mp4(path)
    except (struct.error, IndexError) as exc:
        raise VideoCheckError(f"corrupt MP4: {exc}") from exc
    handlers = [t["handler"] for t in info.tracks]
    if "vide" not in handlers:
        raise VideoCheckError("no video track")
    if "soun" not in handlers:
        raise VideoCheckError("no audio track (the narration is missing)")
    want = FRAME_SIZES.get(aspect)
    if want and info.frame_size != want:
        raise VideoCheckError(f"frame is {info.frame_size[0]}x{info.frame_size[1]}, expected {want[0]}x{want[1]}")
    low = max(MIN_SECONDS, words * SECONDS_PER_WORD[0])
    high = words * SECONDS_PER_WORD[1] + 15 if words else MAX_SECONDS
    if info.duration_s < low:
        raise VideoCheckError(f"video is too short ({info.duration_s:.1f}s, expected at least {low:.0f}s)")
    if info.duration_s > high:
        raise VideoCheckError(f"video is too long ({info.duration_s:.1f}s, expected at most {high:.0f}s)")
    return info
