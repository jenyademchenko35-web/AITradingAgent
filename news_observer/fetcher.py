"""Shared stdlib HTTP fetcher with retry and gzip handling."""

from __future__ import annotations

import gzip
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .models import FetchResponse, SourceConfig


class FetchError(RuntimeError):
    """Raised when one source cannot be fetched successfully."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        attempts: int = 0,
        error_type: str = "HTTP_ERROR",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts
        self.error_type = error_type


class HTTPFetcher:
    """Fetch one source payload with bounded retries and body size."""

    def fetch(self, source: SourceConfig) -> FetchResponse:
        """Fetch a payload or raise FetchError."""
        parsed_location = urllib.parse.urlsplit(source.location)
        if parsed_location.scheme not in {"http", "https"} or not parsed_location.netloc:
            raise FetchError(
                "only http/https sources are allowed",
                attempts=0,
                error_type="HTTP_ERROR",
            )
        attempts = 0
        last_error: Exception | None = None
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
            "User-Agent": source.user_agent,
        }
        headers.update(dict(source.headers))

        for attempt in range(source.retries + 1):
            attempts = attempt + 1
            request = urllib.request.Request(source.location, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=source.timeout) as response:
                    body = response.read(source.max_body_bytes + 1)
                    if len(body) > source.max_body_bytes:
                        raise FetchError(
                            f"response body exceeds {source.max_body_bytes} bytes",
                            status_code=int(getattr(response, "status", 0) or response.getcode() or 0),
                            attempts=attempts,
                            error_type="HTTP_ERROR",
                        )
                    content_encoding = str(response.headers.get("Content-Encoding") or "").lower()
                    if "gzip" in content_encoding:
                        body = gzip.decompress(body)
                        if len(body) > source.max_body_bytes:
                            raise FetchError(
                                f"decompressed body exceeds {source.max_body_bytes} bytes",
                                status_code=int(getattr(response, "status", 0) or response.getcode() or 0),
                                attempts=attempts,
                                error_type="HTTP_ERROR",
                            )
                    final_url = str(getattr(response, "url", source.location))
                    final_parts = urllib.parse.urlsplit(final_url)
                    if final_parts.scheme not in {"http", "https"} or not final_parts.netloc:
                        raise FetchError(
                            "redirected to an unsupported URL",
                            attempts=attempts,
                            error_type="HTTP_ERROR",
                        )
                    return FetchResponse(
                        final_url=final_url,
                        status_code=int(getattr(response, "status", 0) or response.getcode() or 200),
                        headers=dict(response.headers.items()),
                        body=body,
                        attempts=attempts,
                    )
            except FetchError as exc:
                last_error = exc
            except urllib.error.HTTPError as exc:
                last_error = FetchError(
                    f"HTTP {exc.code}: {exc.reason}",
                    status_code=int(exc.code or 0),
                    attempts=attempts,
                    error_type="HTTP_ERROR",
                )
            except (socket.timeout, TimeoutError) as exc:
                last_error = FetchError(
                    str(exc) or "request timeout",
                    attempts=attempts,
                    error_type="TIMEOUT",
                )
            except urllib.error.URLError as exc:
                reason = exc.reason
                is_timeout = isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower()
                last_error = FetchError(
                    str(exc),
                    attempts=attempts,
                    error_type="TIMEOUT" if is_timeout else "HTTP_ERROR",
                )
            except (OSError, gzip.BadGzipFile) as exc:
                last_error = FetchError(
                    str(exc),
                    attempts=attempts,
                    error_type="HTTP_ERROR",
                )
            if attempt < source.retries:
                time.sleep(source.backoff_seconds * (attempt + 1))

        if isinstance(last_error, FetchError):
            raise last_error
        status_code = int(getattr(last_error, "code", 0) or 0)
        raise FetchError(
            str(last_error or "unknown fetch error"),
            status_code=status_code,
            attempts=attempts,
            error_type="HTTP_ERROR",
        )
