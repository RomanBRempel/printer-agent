"""Pulling a print file from the hub and handing it to the printer.

`file_offer` is a pull: the hub names a URL and a checksum, the agent fetches it
with the same bearer token the socket uses, verifies it, and only then lets the
adapter push it to the printer. Nothing here knows a vendor protocol — the
upload itself is the adapter's business.

Three rules shape the code:

- **the body never enters memory.** A gcode file runs to hundreds of megabytes;
  it is streamed to a partial file and hashed on the way past;
- **verification precedes the printer.** A checksum mismatch means the file is
  not the one the job was planned for, and printing it would put the wrong part
  on the bed. It fails loudly and deletes the download;
- **only `503` is worth retrying.** The other refusals name a cause that a
  repeat cannot change (see the table in the contract document).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from ..adapters.base import PrinterAdapter, UnsupportedCommandError
from ..core.filecache import InvalidFileRef, PrintFileCache

logger = logging.getLogger(__name__)

#: Chunk size for streaming the body to disk. Large enough that a 200 MB file is
#: a few hundred reads, small enough to stay off the large-object heap.
DOWNLOAD_CHUNK_BYTES = 1024 * 1024

#: Longest a single read may stall before the transfer is considered dead.
SOCKET_READ_TIMEOUT_S = 120

#: Hub statuses that a repeat can never turn into a file.
TERMINAL_STATUSES = {401, 403, 404, 409}

RETRY_BACKOFF_S = (2.0, 5.0, 15.0)


class FileOfferError(RuntimeError):
    """The offer could not be turned into a file on the printer."""


class PrintFileService:
    def __init__(self, cache: PrintFileCache, agent_token: str):
        self._cache = cache
        self._agent_token = agent_token

    def has_cached(self, file_ref: str) -> bool:
        return self._cache.has(file_ref)

    async def accept(self, adapter: PrinterAdapter, offer: dict[str, Any]) -> dict[str, Any]:
        """Fetch, verify, cache and upload one offered file.

        Returns the `command_result` response body. Raises
        :class:`UnsupportedCommandError` when the adapter cannot take files at
        all, and :class:`FileOfferError` for everything the operator has to see.
        """
        file_ref = str(offer.get("file_ref", "")).strip()
        url = str(offer.get("url", "")).strip()
        sha256 = str(offer.get("sha256", "")).strip().lower()
        remote_name = str(offer.get("remote_name", "")).strip() or file_ref
        size_bytes = _optional_int(offer.get("size_bytes"))

        if not file_ref:
            raise FileOfferError("file_offer without file_ref: nothing would name the cached file")
        if not url:
            raise FileOfferError(f"file_offer {file_ref} without url")
        if not sha256:
            raise FileOfferError(f"file_offer {file_ref} without sha256: the file cannot be verified")
        try:
            self._cache.path_for(file_ref)
        except InvalidFileRef as exc:
            raise FileOfferError(str(exc)) from exc

        if not adapter.capabilities().upload:
            raise UnsupportedCommandError(
                f"{adapter.printer.brand} cannot accept uploaded files on this agent"
            )

        cached = await asyncio.to_thread(self._reuse_cached, file_ref, sha256, size_bytes)
        if cached:
            logger.info(
                "print file already cached",
                extra={"action": "file_offer", "printer_key": adapter.printer_key, "file_ref": file_ref},
            )
        else:
            await self._download(file_ref, url, sha256, size_bytes, deadline=_deadline(offer))
            await asyncio.to_thread(self._cache.prune)

        path = self._cache.path_for(file_ref)
        upload_response = await adapter.upload_file(path, remote_name)
        logger.info(
            "print file delivered to the printer",
            extra={
                "action": "file_offer",
                "printer_key": adapter.printer_key,
                "file_ref": file_ref,
                "remote_name": remote_name,
            },
        )
        return {
            "file_ref": file_ref,
            "remote_name": remote_name,
            "sha256": sha256,
            "size_bytes": self._cache.size_of(file_ref),
            "reused_cached_file": cached,
            "upload": upload_response if isinstance(upload_response, dict) else {},
        }

    # -- download ------------------------------------------------------

    def _reuse_cached(self, file_ref: str, sha256: str, size_bytes: int | None) -> bool:
        """Answer whether the cache already holds exactly this file.

        Re-downloading a file the agent already verified wastes the link a shop
        floor usually has least of; a wrong file under the right name is worse
        than a slow download, so the copy is re-hashed rather than trusted.
        """
        if not self._cache.has(file_ref):
            return False
        path = self._cache.path_for(file_ref)
        if size_bytes is not None and path.stat().st_size != size_bytes:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK_BYTES), b""):
                digest.update(chunk)
        return digest.hexdigest() == sha256

    async def _download(
        self,
        file_ref: str,
        url: str,
        sha256: str,
        size_bytes: int | None,
        *,
        deadline: datetime | None,
    ) -> None:
        attempt = 0
        while True:
            try:
                await self._download_once(file_ref, url, sha256, size_bytes)
                return
            except _RetryableFetch as exc:
                if attempt >= len(RETRY_BACKOFF_S) or _expired(deadline):
                    raise FileOfferError(
                        f"hub could not serve {file_ref}: {exc}"
                    ) from exc
                delay = RETRY_BACKOFF_S[attempt]
                logger.warning(
                    "hub could not serve the print file yet",
                    extra={"action": "file_offer", "file_ref": file_ref, "error": str(exc)},
                )
                await asyncio.sleep(delay)
                attempt += 1

    async def _download_once(
        self, file_ref: str, url: str, sha256: str, size_bytes: int | None
    ) -> None:
        partial = self._cache.partial_path_for(file_ref)
        digest = hashlib.sha256()
        written = 0
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=SOCKET_READ_TIMEOUT_S)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=self._headers()) as response:
                    if response.status in TERMINAL_STATUSES:
                        raise FileOfferError(
                            f"hub refused {file_ref} with HTTP {response.status}: "
                            f"{_reason(response.status)}"
                        )
                    if response.status >= 400:
                        raise _RetryableFetch(f"HTTP {response.status}")

                    announced = response.headers.get("X-Print-File-Sha256", "").strip().lower()
                    if announced and announced != sha256:
                        # The hub is serving a different revision than the command
                        # was issued for; downloading it would only waste the link.
                        raise FileOfferError(
                            f"hub serves {file_ref} with checksum {announced}, "
                            f"the command asked for {sha256}"
                        )

                    with partial.open("wb") as handle:
                        async for chunk in response.content.iter_chunked(DOWNLOAD_CHUNK_BYTES):
                            handle.write(chunk)
                            digest.update(chunk)
                            written += len(chunk)
        except aiohttp.ClientError as exc:
            self._cache.discard(partial)
            raise _RetryableFetch(str(exc) or exc.__class__.__name__) from exc
        except BaseException:
            self._cache.discard(partial)
            raise

        if size_bytes is not None and written != size_bytes:
            self._cache.discard(partial)
            raise FileOfferError(
                f"{file_ref} is {written} bytes, the offer announced {size_bytes}"
            )
        if digest.hexdigest() != sha256:
            self._cache.discard(partial)
            raise FileOfferError(
                f"{file_ref} failed its checksum: got {digest.hexdigest()}, expected {sha256}"
            )
        self._cache.commit(partial, file_ref)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._agent_token}"}


class _RetryableFetch(RuntimeError):
    """A transport hiccup or a `503`: the same request may still succeed."""


def _reason(status: int) -> str:
    return {
        401: "the agent token was not accepted",
        403: "the file belongs to a printer of another agent",
        404: "the file is unknown or was already cleaned up",
        409: "the source file changed after the command was issued",
    }.get(status, "refused")


def _optional_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _deadline(offer: dict[str, Any]) -> datetime | None:
    return parse_iso8601(str(offer.get("expires_at", "")))


def _expired(deadline: datetime | None) -> bool:
    return deadline is not None and datetime.now(timezone.utc) >= deadline


def parse_iso8601(value: str) -> datetime | None:
    """Parse a contract timestamp, tolerating the `Z` the wire uses."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
