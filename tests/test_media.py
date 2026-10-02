"""Downloading media: what is accepted, what is refused, and what is left behind.

Every test here runs against a fake transport. Nothing opens a socket — the
whole suite must pass in a process with no network at all (see `conftest`), and
the properties under test are about *decisions* rather than about the CDN:

* the declared `Content-Type` is never trusted; the sniffed bytes decide — so a
  JPEG arriving where a video was expected is a rejection, not an upload under
  the wrong kind;
* a refusal leaves no file in the workspace, so a retry cannot pick up a partial
  download;
* the size ceiling is enforced twice — against a declared `Content-Length` and
  again on every chunk — because a chunked response need never declare one;
* a host outside the allowlist is refused before a connection is attempted.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from app.config import Settings
from app.constants import MAX_FILE_UPLOAD_BYTES, MAX_PHOTO_UPLOAD_BYTES
from app.database.enums import MediaKind
from app.providers.base import MediaData
from app.services.media import (
    DownloadedMedia,
    MediaDownloader,
    MediaFetchError,
    MediaRejectedError,
    MediaTooLargeError,
    UnsupportedMediaError,
)

# ------------------------------------------------------------------- payloads
#: The smallest byte strings that `sniff_mime` identifies as each format.
JPEG = b"\xff\xd8\xff" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
MOV = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 64
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
NOT_MEDIA = b"<html>this is not media at all</html>"


# ---------------------------------------------------------------------- fakes
class FakeContent:
    """`aiohttp.StreamReader` reduced to the one method `_write` calls."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.consumed = False

    async def iter_chunked(self, size: int) -> Any:
        self.consumed = True
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    """Enough of a `ClientResponse` for `fetch()` and `_write()`."""

    def __init__(
        self,
        body: bytes = b"",
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        # A single chunk unless the test needs to control the stream shape.
        self.content = FakeContent(chunks if chunks is not None else [body])

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class FakeSession:
    """Records requests, and can raise instead of answering one."""

    def __init__(self, response: FakeResponse | Exception) -> None:
        self._response = response
        self.requests: list[str] = []

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append(url)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def close(self) -> None:
        return None


class CancellingContent:
    """A stream that dies mid-transfer, as a worker shutdown cancels one."""

    async def iter_chunked(self, size: int) -> Any:
        yield b"\x00" * 32
        raise asyncio.CancelledError


def cancelling_response() -> FakeResponse:
    response = FakeResponse()
    response.content = CancellingContent()  # type: ignore[assignment]
    return response


def make_downloader(
    settings: Settings, response: FakeResponse | Exception
) -> tuple[MediaDownloader, FakeSession]:
    """A downloader wired to a fake transport.

    The session is injected rather than entered: `__aenter__` would build a real
    `aiohttp.ClientSession` with an SSRF-pinning connector, which is exactly what
    these tests exist to avoid needing.
    """
    downloader = MediaDownloader(settings)
    session = FakeSession(response)
    downloader._session = session  # type: ignore[assignment]
    return downloader, session


def video(remote_url: str = "https://video.twimg.com/clip.mp4") -> MediaData:
    return MediaData(
        position=0,
        kind=MediaKind.VIDEO,
        remote_url=remote_url,
        content_type="video/mp4",
        duration_ms=15_000,
    )


def photo(remote_url: str = "https://pbs.twimg.com/media/a.jpg") -> MediaData:
    return MediaData(position=0, kind=MediaKind.PHOTO, remote_url=remote_url)


def with_settings(base: Settings, **updates: object) -> Settings:
    """Vary one setting without re-validating the whole model (see test_render)."""
    return base.model_copy(update=updates)


def contents(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.is_file())


# ------------------------------------------------------------------ happy path
async def test_a_video_is_written_with_the_extension_its_bytes_imply(
    settings: Settings, tmp_path: Path
) -> None:
    downloader, session = make_downloader(settings, FakeResponse(MP4))

    downloaded = await downloader.download_video(video(), tmp_path)

    assert session.requests == ["https://video.twimg.com/clip.mp4"]
    assert downloaded.mime == "video/mp4"
    assert downloaded.path.suffix == ".mp4"
    assert downloaded.size_bytes == len(MP4)
    assert downloaded.sha256 == hashlib.sha256(MP4).hexdigest()
    assert downloaded.path.read_bytes() == MP4


async def test_a_mov_container_is_a_video_the_bot_may_upload(
    settings: Settings, tmp_path: Path
) -> None:
    """`video/quicktime` is on the accepted list, so it is not treated as junk."""
    downloader, _ = make_downloader(settings, FakeResponse(MOV))

    downloaded = await downloader.download_video(
        video("https://video.twimg.com/clip.mov"), tmp_path
    )

    assert downloaded.mime == "video/quicktime"
    assert downloaded.path.suffix == ".mov"


async def test_a_photo_is_still_downloaded_through_the_same_path(
    settings: Settings, tmp_path: Path
) -> None:
    downloader, _ = make_downloader(settings, FakeResponse(JPEG))

    downloaded = await downloader.download_photo(photo(), tmp_path)

    assert downloaded.mime == "image/jpeg"
    assert downloaded.path.suffix == ".jpg"


# ------------------------------------------------------- the kind must match
async def test_a_jpeg_offered_where_a_video_was_expected_is_rejected(
    settings: Settings, tmp_path: Path
) -> None:
    """A provider that hands over the poster frame must not upload it as a video."""
    downloader, _ = make_downloader(
        settings, FakeResponse(JPEG, headers={"Content-Type": "video/mp4"})
    )

    with pytest.raises(UnsupportedMediaError) as excinfo:
        await downloader.download_video(video(), tmp_path)

    assert "video" in str(excinfo.value)
    assert contents(tmp_path) == []


async def test_an_mp4_offered_where_a_photo_was_expected_is_rejected(
    settings: Settings, tmp_path: Path
) -> None:
    downloader, _ = make_downloader(
        settings, FakeResponse(MP4, headers={"Content-Type": "image/jpeg"})
    )

    with pytest.raises(UnsupportedMediaError) as excinfo:
        await downloader.download_photo(photo(), tmp_path)

    assert "image" in str(excinfo.value)
    assert contents(tmp_path) == []


async def test_an_unrecognised_body_is_rejected_rather_than_trusted(
    settings: Settings, tmp_path: Path
) -> None:
    """The declared type is a claim by the server, and a server can lie."""
    downloader, _ = make_downloader(
        settings, FakeResponse(NOT_MEDIA, headers={"Content-Type": "image/jpeg"})
    )

    with pytest.raises(UnsupportedMediaError) as excinfo:
        await downloader.download_photo(photo(), tmp_path)

    assert "not a recognised image" in str(excinfo.value)
    assert contents(tmp_path) == []


async def test_a_webm_video_is_accepted(settings: Settings, tmp_path: Path) -> None:
    downloader, _ = make_downloader(settings, FakeResponse(WEBM))

    downloaded = await downloader.download_video(
        video("https://video.twimg.com/clip.webm"), tmp_path
    )

    assert downloaded.mime == "video/webm"


# ------------------------------------------------------------------- ceilings
async def test_a_declared_length_over_the_ceiling_is_refused_before_the_body(
    settings: Settings, tmp_path: Path
) -> None:
    """Refusing early is the point: no bandwidth is spent on bytes we cannot send."""
    response = FakeResponse(MP4, headers={"Content-Length": "999999999"})
    downloader, _ = make_downloader(settings, response)

    with pytest.raises(MediaTooLargeError):
        await downloader.fetch(
            "https://video.twimg.com/clip.mp4",
            tmp_path,
            max_bytes=1024,
            allowed_mimes=frozenset({"video/mp4"}),
            label="video",
        )

    assert response.content.consumed is False
    assert contents(tmp_path) == []


async def test_a_body_that_never_declares_its_size_is_stopped_mid_transfer(
    settings: Settings, tmp_path: Path
) -> None:
    """A chunked response need not declare anything; the running total decides."""
    chunks = [b"\x00" * 512, b"\x00" * 512, b"\x00" * 512]
    downloader, _ = make_downloader(settings, FakeResponse(chunks=chunks))

    with pytest.raises(MediaTooLargeError):
        await downloader.fetch(
            "https://video.twimg.com/clip.mp4",
            tmp_path,
            max_bytes=1024,
            allowed_mimes=frozenset({"video/mp4"}),
            label="video",
        )

    assert contents(tmp_path) == []


def test_the_video_ceiling_is_the_bot_api_file_limit(settings: Settings) -> None:
    downloader = MediaDownloader(settings)

    # The default MEDIA_MAX_SIZE_MB is 45, below the 50 MB file limit.
    assert downloader.max_video_bytes == settings.media_max_bytes
    assert downloader.max_video_bytes == min(settings.media_max_bytes, MAX_FILE_UPLOAD_BYTES)


def test_the_photo_ceiling_is_capped_by_the_bot_api_photo_limit(
    settings: Settings,
) -> None:
    """45 MB is a legal *file* upload and an illegal *photo* one."""
    downloader = MediaDownloader(with_settings(settings, MEDIA_MAX_SIZE_MB=45))

    assert downloader.max_photo_bytes == MAX_PHOTO_UPLOAD_BYTES
    assert downloader.max_photo_bytes == min(45 * 1024 * 1024, MAX_PHOTO_UPLOAD_BYTES)


def test_a_lowered_setting_lowers_both_ceilings(settings: Settings) -> None:
    downloader = MediaDownloader(with_settings(settings, MEDIA_MAX_SIZE_MB=1))

    assert downloader.max_video_bytes == 1 * 1024 * 1024
    assert downloader.max_photo_bytes == 1 * 1024 * 1024


# ------------------------------------------------------------------- failures
async def test_a_non_200_is_a_retryable_fetch_failure(
    settings: Settings, tmp_path: Path
) -> None:
    downloader, _ = make_downloader(settings, FakeResponse(status=404))

    with pytest.raises(MediaFetchError) as excinfo:
        await downloader.download_video(video(), tmp_path)

    assert excinfo.value.retryable is True
    assert "404" in str(excinfo.value)


async def test_a_transport_error_is_a_retryable_fetch_failure(
    settings: Settings, tmp_path: Path
) -> None:
    downloader, _ = make_downloader(settings, aiohttp.ClientConnectionError("reset by peer"))

    with pytest.raises(MediaFetchError) as excinfo:
        await downloader.download_video(video(), tmp_path)

    assert excinfo.value.retryable is True


async def test_a_timeout_is_a_retryable_fetch_failure(
    settings: Settings, tmp_path: Path
) -> None:
    downloader, _ = make_downloader(settings, TimeoutError())

    with pytest.raises(MediaFetchError) as excinfo:
        await downloader.download_video(video(), tmp_path)

    assert excinfo.value.retryable is True
    assert "timed out" in str(excinfo.value)


async def test_a_host_outside_the_allowlist_never_reaches_the_network(
    settings: Settings, tmp_path: Path
) -> None:
    downloader, session = make_downloader(settings, FakeResponse(MP4))

    with pytest.raises(MediaRejectedError) as excinfo:
        await downloader.download_video(video("https://evil.example/clip.mp4"), tmp_path)

    assert session.requests == []
    assert excinfo.value.retryable is False


async def test_a_video_with_no_url_is_a_rejection_rather_than_a_request(
    settings: Settings, tmp_path: Path
) -> None:
    """An MP4-less video is a scope limit the renderer already counted. The
    downloader must not turn it into a transport failure by fetching ""."""
    downloader, session = make_downloader(settings, FakeResponse(MP4))

    with pytest.raises(MediaRejectedError):
        await downloader.download_video(video(""), tmp_path)

    assert session.requests == []


async def test_a_cancelled_download_leaves_no_partial_file(
    settings: Settings, tmp_path: Path
) -> None:
    """A job interrupted mid-transfer must not leave bytes for the retry to
    mistake for a complete download."""
    downloader, _ = make_downloader(settings, cancelling_response())

    with pytest.raises(asyncio.CancelledError):
        await downloader.download_video(video(), tmp_path)

    assert contents(tmp_path) == []


# ------------------------------------------------------------- the filesystem
async def test_the_filename_is_random_rather_than_taken_from_the_url(
    settings: Settings, tmp_path: Path
) -> None:
    """Path traversal is structurally impossible: the URL never names the file."""
    downloader, _ = make_downloader(settings, FakeResponse(MP4))

    downloaded = await downloader.download_video(
        video("https://video.twimg.com/../../etc/passwd.mp4"), tmp_path
    )

    assert downloaded.path.parent == tmp_path
    assert downloaded.path.name.startswith("media_")
    assert downloaded.path.name != "passwd.mp4"


async def test_fetch_without_the_context_manager_is_a_programming_error(
    settings: Settings, tmp_path: Path
) -> None:
    """Nothing may create the session implicitly — aiohttp binds it to a loop."""
    downloader = MediaDownloader(settings)

    with pytest.raises(RuntimeError):
        await downloader.download_video(video(), tmp_path)


def test_downloaded_media_is_immutable() -> None:
    media = DownloadedMedia(
        path=Path("/tmp/a.mp4"), mime="video/mp4", size_bytes=1, sha256="x"
    )

    with pytest.raises(FrozenInstanceError):
        media.mime = "video/webm"  # type: ignore[misc]
