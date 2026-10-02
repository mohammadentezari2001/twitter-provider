"""Safe temporary-file handling and content-type sniffing.

Filenames are *always* generated from a random token plus an extension derived
from sniffed content. User-supplied names and URL paths never reach the
filesystem, which makes path traversal structurally impossible rather than
something we have to filter for.
"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.constants import MIME_EXTENSIONS

#: (offset, signature, mime) triples checked in order.
_SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"GIF87a", "image/gif"),
    (0, b"GIF89a", "image/gif"),
    (0, b"\x1a\x45\xdf\xa3", "video/webm"),
)

#: `ftyp` brands that identify an ISO-BMFF container we can treat as MP4/MOV.
_FTYP_BRANDS = {
    b"isom": "video/mp4",
    b"iso2": "video/mp4",
    b"mp41": "video/mp4",
    b"mp42": "video/mp4",
    b"avc1": "video/mp4",
    b"dash": "video/mp4",
    b"M4V ": "video/mp4",
    b"qt  ": "video/quicktime",
}


def sniff_mime(head: bytes) -> str | None:
    """Identify a media type from the first bytes of a file.

    Returns `None` when the content is not a format we support, which the
    caller must treat as a rejection rather than a fallback to the declared
    `Content-Type` (a server could lie about it).
    """
    if not head:
        return None

    for offset, signature, mime in _SIGNATURES:
        if head[offset : offset + len(signature)] == signature:
            return mime

    # RIFF....WEBP
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"

    # ....ftyp<brand>
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _FTYP_BRANDS:
            return _FTYP_BRANDS[brand]
        # Unknown brand but a valid ISO-BMFF header: MP4 is the safe assumption.
        return "video/mp4"

    return None


def extension_for(mime: str | None) -> str:
    return MIME_EXTENSIONS.get((mime or "").lower(), ".bin")


def random_filename(mime: str | None = None, *, prefix: str = "") -> str:
    """Unpredictable, collision-free filename with a safe extension."""
    token = secrets.token_hex(12)
    clean_prefix = "".join(c for c in prefix if c.isalnum() or c in "-_")[:24]
    stem = f"{clean_prefix}_{token}" if clean_prefix else token
    return f"{stem}{extension_for(mime)}"


@contextmanager
def temp_workspace(base_dir: Path, *, prefix: str = "job-") -> Iterator[Path]:
    """Private directory removed on exit, whatever happens inside the block.

    Created with mode 0700 by `mkdtemp`, so other containers sharing the volume
    cannot read partially downloaded media.
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=base_dir))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def is_within(child: Path, parent: Path) -> bool:
    """True if `child` resolves inside `parent`. Defence-in-depth assertion."""
    try:
        child.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True


def prune_stale_dirs(base_dir: Path, *, max_age_seconds: float) -> int:
    """Delete leftover workspaces from a crashed process. Returns the count."""
    if not base_dir.exists():
        return 0
    import time

    cutoff = time.time() - max_age_seconds
    removed = 0
    for entry in base_dir.iterdir():
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def stale_workspace_age(job_lock_timeout: float) -> float:
    """How old a leftover media workspace must be before it is deleted.

    Comfortably older than any publish could still be running, so a slow upload
    is never removed underneath itself. Tied to `JOB_LOCK_TIMEOUT` because that
    is the process's own stated answer to "how long may one job legitimately
    take?" — the two would otherwise drift and the pruner would eventually start
    deleting work out from under a live publish.
    """
    return max(3600.0, job_lock_timeout * 2.0)


def human_size(num_bytes: int | None) -> str:
    if num_bytes is None:
        return "—"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def ensure_dir(path: Path, *, mode: int = 0o700) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass  # non-owner on a mounted volume; not fatal
    return path
