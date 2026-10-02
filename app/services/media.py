"""Downloading photos and video, safely.

Three properties this module guarantees, each of which is a bug if absent:

* **The bytes are what they claim to be.** The declared `Content-Type` is
  ignored entirely; the format is decided by `sniff_mime()` on the first bytes
  actually received. A server that says `image/jpeg` and sends HTML gets
  rejected, which matters because the file is uploaded to Telegram under our
  name. The same check is what keeps a photo download from accepting an MP4 and
  a video download from accepting a JPEG.
* **The URL cannot reach the local network.** Every download goes through the
  same `UrlPolicy`/`SafeResolver` used elsewhere, so a provider whose media URL
  points at `169.254.169.254` — the cloud metadata endpoint — is refused before
  a connection is made, and a redirect to it is refused during the redirect.
* **The filename is never derived from the URL.** `random_filename()` builds it
  from a random token plus an extension from the *sniffed* type, so path
  traversal is structurally impossible rather than filtered against.

The size ceiling is per kind and is the *smaller* of `MEDIA_MAX_SIZE_MB` and
Telegram's own limit for that upload — 10 MB for a photo, 50 MB for a file.
Downloading 40 MB and then having Telegram reject the upload wastes the
bandwidth and produces a confusing error; refusing early produces a clear one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from app.config import Settings
from app.constants import (
    MAX_FILE_UPLOAD_BYTES,
    MAX_PHOTO_UPLOAD_BYTES,
    MIME_SNIFF_BYTES,
    SUPPORTED_IMAGE_MIMES,
    SUPPORTED_VIDEO_MIMES,
)
from app.logging_config import get_logger
from app.providers.base import MediaData
from app.utils.files import extension_for, is_within, random_filename, sniff_mime
from app.utils.net import UnsafeUrlError, UrlPolicy, create_session

log = get_logger(__name__)

_CHUNK_BYTES = 64 * 1024


class MediaError(Exception):
    """Base for every media failure.

    `retryable` mirrors `ProviderError`: the publisher maps it onto the job's
    retry decision without knowing anything about HTTP.
    """

    retryable = True


class MediaRejectedError(MediaError):
    """The URL failed the SSRF policy. Not retryable — the answer will not change."""

    retryable = False


class MediaTooLargeError(MediaError):
    """Over the size ceiling. Not retryable: the file will not shrink."""

    retryable = False


class UnsupportedMediaError(MediaError):
    """The content is not a format we publish. Not retryable."""

    retryable = False


class MediaFetchError(MediaError):
    """The transfer itself failed: timeout, reset, non-200. Retryable."""

    retryable = True


@dataclass(frozen=True, slots=True)
class DownloadedMedia:
    path: Path
    mime: str
    size_bytes: int
    sha256: str


class MediaDownloader:
    """Downloads photos and video into a per-job workspace.

    Async context manager, because `aiohttp.ClientSession` must be constructed
    with a running event loop. Creating it in `__init__` would either bind it to
    whichever loop happened to be current or raise, depending on the aiohttp
    version — neither is something the caller should have to reason about.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._policy = UrlPolicy(
            allowed_hosts=settings.MEDIA_ALLOWED_HOSTS,
            allow_private=settings.ALLOW_PRIVATE_MEDIA_HOSTS,
        )
        self._session: aiohttp.ClientSession | None = None

    @property
    def max_photo_bytes(self) -> int:
        """The effective ceiling: our setting, capped by Telegram's own limit."""
        return min(self._settings.media_max_bytes, MAX_PHOTO_UPLOAD_BYTES)

    @property
    def max_video_bytes(self) -> int:
        """Video is uploaded with `sendVideo`, so the file limit applies.

        Telegram's Bot API caps bot uploads at 50 MB; `MEDIA_MAX_SIZE_MB` is
        validated to at most 50, so the two agree unless the operator lowered it.
        """
        return min(self._settings.media_max_bytes, MAX_FILE_UPLOAD_BYTES)

    async def __aenter__(self) -> MediaDownloader:
        self._session = create_session(
            self._policy,
            connect_timeout=self._settings.MEDIA_CONNECT_TIMEOUT,
            total_timeout=self._settings.MEDIA_DOWNLOAD_TIMEOUT,
            limit=4,
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------ fetch

    async def download_photo(self, media: MediaData, dest_dir: Path) -> DownloadedMedia:
        """Fetch one photo into `dest_dir`. Raises `MediaError` on any failure."""
        return await self.fetch(
            media.remote_url,
            dest_dir,
            max_bytes=self.max_photo_bytes,
            allowed_mimes=SUPPORTED_IMAGE_MIMES,
            label="image",
        )

    async def download_video(self, media: MediaData, dest_dir: Path) -> DownloadedMedia:
        """Fetch one video into `dest_dir`. Raises `MediaError` on any failure.

        `media.remote_url` is the MP4 rendition the provider selected; an HLS
        manifest is never used because Telegram uploads a file, not a stream.
        """
        return await self.fetch(
            media.remote_url,
            dest_dir,
            max_bytes=self.max_video_bytes,
            allowed_mimes=SUPPORTED_VIDEO_MIMES,
            label="video",
        )

    async def fetch(
        self,
        url: str,
        dest_dir: Path,
        *,
        max_bytes: int,
        allowed_mimes: frozenset[str] = SUPPORTED_IMAGE_MIMES,
        label: str = "image",
    ) -> DownloadedMedia:
        if self._session is None:
            raise RuntimeError("MediaDownloader must be used as an async context manager")

        try:
            self._policy.ensure_url_allowed(url)
        except UnsafeUrlError as exc:
            raise MediaRejectedError(f"media URL rejected: {exc}") from exc

        try:
            response = await self._session.get(url, allow_redirects=True)
            async with response:
                if response.status != 200:
                    raise MediaFetchError(
                        f"media server returned {response.status}",
                    )
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise MediaTooLargeError(
                        f"media is {int(declared)} bytes, over the {max_bytes} byte ceiling"
                    )
                return await self._write(
                    response,
                    dest_dir,
                    max_bytes=max_bytes,
                    allowed_mimes=allowed_mimes,
                    label=label,
                )
        except MediaError:
            raise
        except UnsafeUrlError as exc:
            # Raised by the resolver mid-flight, including on a redirect.
            raise MediaRejectedError(f"media URL rejected during transfer: {exc}") from exc
        except aiohttp.ClientError as exc:
            raise MediaFetchError(f"media transfer failed: {exc!r}") from exc
        except TimeoutError as exc:
            raise MediaFetchError("media transfer timed out") from exc

    async def _write(
        self,
        response: aiohttp.ClientResponse,
        dest_dir: Path,
        *,
        max_bytes: int,
        allowed_mimes: frozenset[str],
        label: str,
    ) -> DownloadedMedia:
        """Stream to disk, then validate what actually arrived.

        Written incrementally rather than accumulated in memory: at
        `MAX_CONCURRENT_FETCHES` downloads of up to 50 MB each, buffering would
        put hundreds of megabytes of transient data in the heap for no reason.
        The file is removed again if the content turns out not to be usable.
        """
        digest = hashlib.sha256()
        head = bytearray()
        total = 0

        dest_dir.mkdir(parents=True, exist_ok=True)
        # The extension is provisional — `sniff_mime` decides the real type, and
        # the file is renamed below if it disagrees with this guess.
        path = dest_dir / random_filename(None, prefix="media")
        if not is_within(path, dest_dir):  # pragma: no cover - random_filename cannot escape
            raise MediaRejectedError("refusing to write outside the workspace")

        try:
            with path.open("wb") as handle:
                async for chunk in response.content.iter_chunked(_CHUNK_BYTES):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        # Checked on every chunk, not just against Content-Length:
                        # a chunked response need never declare its size, and
                        # trusting the header alone would let an unbounded body
                        # fill the disk.
                        raise MediaTooLargeError(
                            f"media exceeded the {max_bytes} byte ceiling mid-transfer"
                        )
                    if len(head) < MIME_SNIFF_BYTES:
                        head.extend(chunk[: MIME_SNIFF_BYTES - len(head)])
                    digest.update(chunk)
                    handle.write(chunk)

            mime = sniff_mime(bytes(head))
            if mime is None:
                raise UnsupportedMediaError(
                    f"media content is not a recognised {label} "
                    f"(first bytes {bytes(head[:12])!r})"
                )
            if mime not in allowed_mimes:
                # A JPEG arriving where a video was expected (or the reverse) is
                # rejected here rather than uploaded under the wrong kind. The
                # message names the type so the operator knows it is a mismatch
                # rather than a corrupt file.
                raise UnsupportedMediaError(
                    f"media type {mime} is not a {label} this build publishes"
                )

            final = path.with_suffix(extension_for(mime))
            if final != path:
                path.replace(final)
                path = final
        except BaseException:
            # Includes cancellation: a job interrupted mid-download must not
            # leave a partial file for the next attempt to pick up.
            path.unlink(missing_ok=True)
            raise

        return DownloadedMedia(
            path=path, mime=mime, size_bytes=total, sha256=digest.hexdigest()
        )
