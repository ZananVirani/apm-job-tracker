from __future__ import annotations

import json
import http.cookiejar
from http.client import IncompleteRead
from pathlib import Path
import random
import os
import shutil
import ssl
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener, urlopen
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .models import FetchResult


USER_AGENT = "apm-notifier/0.3 (personal internship alert bot; polite, respects robots.txt)"
DEFAULT_HEADERS = {
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.8",
    "Cache-Control": "no-cache",
    "User-Agent": USER_AGENT,
}
# Minimum spacing between requests to one host; LinkedIn's guest feed is stricter.
DEFAULT_HOST_INTERVAL_SECONDS = 1.0
SLOW_HOSTS = {"linkedin.com": 3.0, "apply.careers.microsoft.com": 3.0}
BLOCK_MARKERS = (
    "access denied",
    "attention required! | cloudflare",
    "please enable javascript and cookies to continue",
    "request unsuccessful. incapsula incident id",
    "<title>just a moment",
)


class FetchError(RuntimeError):
    pass


def trusted_ssl_context() -> ssl.SSLContext:
    """Build a verified TLS context, including framework Pythons without a CA path."""
    defaults = ssl.get_default_verify_paths()
    candidates = (
        defaults.cafile,
        defaults.openssl_cafile,
        "/etc/ssl/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return ssl.create_default_context(cafile=candidate)
    return ssl.create_default_context()


class HttpClient:
    def __init__(
        self,
        timeout_seconds: int,
        attempts: int = 3,
        host_interval_seconds: float = DEFAULT_HOST_INTERVAL_SECONDS,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts
        self.host_interval_seconds = host_interval_seconds
        self.ssl_context = trusted_ssl_context()
        self._pace_lock = threading.Lock()
        self._next_request: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()

    def fetch(
        self,
        url: str,
        extra_headers: dict[str, str] | None = None,
        method: str = "GET",
        body: str = "",
    ) -> FetchResult:
        headers = dict(DEFAULT_HEADERS)
        headers.update(extra_headers or {})
        encoded_body = body.encode("utf-8") if body else None
        last_error: Exception | None = None
        for attempt in range(self.attempts):
            try:
                self._pace(url)
                request = Request(url, data=encoded_body, headers=headers, method=method)
                with self._open_request(request) as response:
                    response_body = response.read()
                    content_type = response.headers.get("Content-Type", "")
                    charset = response.headers.get_content_charset() or "utf-8"
                    text = response_body.decode(charset, errors="replace")
                    self._check_for_block_page(text, content_type)
                    return FetchResult(
                        requested_url=url,
                        final_url=response.geturl(),
                        content_type=content_type,
                        text=text,
                    )
            except HTTPError as error:
                last_error = error
                if error.code not in {408, 425, 429, 500, 502, 503, 504}:
                    break
                retry_after = error.headers.get("Retry-After", "")
                delay = int(retry_after) if retry_after.isdigit() else 1.5 * (2 ** attempt)
                if attempt + 1 < self.attempts:
                    time.sleep(min(delay, 20) + random.random() / 4)
            except (
                TimeoutError,
                URLError,
                OSError,
                FetchError,
                IncompleteRead,
                json.JSONDecodeError,
            ) as error:
                last_error = error
                if (
                    "CERTIFICATE_VERIFY_FAILED" in str(error) or isinstance(error, IncompleteRead)
                ) and shutil.which("curl"):
                    try:
                        return self._fetch_with_curl(url, headers, method, body)
                    except FetchError as curl_error:
                        last_error = curl_error
                if attempt + 1 < self.attempts:
                    time.sleep(1.5 * (2 ** attempt) + random.random() / 4)
        detail = str(last_error) if last_error else "unknown fetch error"
        raise FetchError(f"{url}: {detail}") from last_error

    def _pace(self, url: str) -> None:
        """Space requests to each host so concurrent sources stay polite."""
        host = (urlsplit(url).hostname or "").casefold()
        interval = self.host_interval_seconds
        for slow_host, slow_interval in SLOW_HOSTS.items():
            if host == slow_host or host.endswith("." + slow_host):
                interval = max(interval, slow_interval)
                host = slow_host
        if interval <= 0:
            return
        while True:
            with self._pace_lock:
                now = time.monotonic()
                ready_at = self._next_request.get(host, 0.0)
                if ready_at <= now:
                    self._next_request[host] = now + interval
                    return
                delay = ready_at - now
            time.sleep(delay)

    def allowed_by_robots(self, url: str) -> bool:
        """Check robots.txt before fetching an HTML job page that is not an official API."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            if origin not in self._robots:
                parser: RobotFileParser | None = RobotFileParser()
                try:
                    response = self.fetch(f"{origin}/robots.txt", {"Accept": "text/plain"})
                    parser.parse(response.text.splitlines())
                except Exception:
                    parser = None  # Missing or unreadable robots.txt imposes no restriction.
                self._robots[origin] = parser
            parser = self._robots[origin]
        return parser is None or parser.can_fetch(USER_AGENT, url)

    def _open_request(self, request: Request):
        """Dayforce's anonymous search needs its normal CSRF token and session cookie."""
        parts = urlsplit(request.full_url)
        if (
            parts.hostname == "jobs.dayforcehcm.com"
            and parts.path.startswith("/api/geo/")
            and request.get_method() == "POST"
        ):
            opener = build_opener(
                HTTPSHandler(context=self.ssl_context),
                HTTPCookieProcessor(http.cookiejar.CookieJar()),
            )
            with opener.open(
                Request("https://jobs.dayforcehcm.com/api/auth/csrf", headers=DEFAULT_HEADERS),
                timeout=self.timeout_seconds,
            ) as response:
                token = json.load(response).get("csrfToken")
            if not isinstance(token, str) or not token:
                raise FetchError("Dayforce did not provide an anonymous search token")
            request.add_header("X-CSRF-TOKEN", token)
            return opener.open(request, timeout=self.timeout_seconds)
        return urlopen(request, timeout=self.timeout_seconds, context=self.ssl_context)

    def url_exists(self, url: str) -> bool:
        """Reject confirmed dead links while treating transient verification failures as live."""
        request = Request(url, headers=DEFAULT_HEADERS, method="HEAD")
        try:
            with urlopen(request, timeout=self.timeout_seconds, context=self.ssl_context) as response:
                return response.status < 400
        except HTTPError as error:
            return error.code not in {404, 410}
        except (TimeoutError, URLError, OSError):
            return True

    def _fetch_with_curl(
        self,
        url: str,
        headers: dict[str, str],
        method: str,
        body: str,
    ) -> FetchResult:
        """Use curl's native trust store when a framework Python lacks an intermediate CA."""
        marker = "\n__APM_NOTIFIER_CURL_METADATA__"
        command = [
            shutil.which("curl") or "curl",
            "--fail",
            "--location",
            "--compressed",
            "--silent",
            "--show-error",
            "--max-time",
            str(self.timeout_seconds),
        ]
        for key, value in headers.items():
            command.extend(("--header", f"{key}: {value}"))
        if method != "GET":
            command.extend(("--request", method))
        if body:
            command.extend(("--data-binary", body))
        command.extend(("--write-out", f"{marker}%{{url_effective}}\t%{{content_type}}", url))
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds + 5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise FetchError(f"{url}: curl fallback failed: {error}") from error
        if completed.returncode != 0:
            raise FetchError(f"{url}: curl fallback failed: {completed.stderr.strip()}")
        if marker not in completed.stdout:
            raise FetchError(f"{url}: curl fallback returned malformed output")
        text, metadata = completed.stdout.rsplit(marker, 1)
        final_url, _, content_type = metadata.partition("\t")
        self._check_for_block_page(text, content_type)
        return FetchResult(
            requested_url=url,
            final_url=final_url or url,
            content_type=content_type,
            text=text,
        )

    @staticmethod
    def _check_for_block_page(text: str, content_type: str) -> None:
        if "html" not in content_type.casefold():
            return
        lowered = text[:200_000].casefold()
        marker = next((item for item in BLOCK_MARKERS if item in lowered), None)
        if marker:
            raise FetchError(f"site returned a bot-protection page ({marker})")


class BrowserRenderer:
    """Render the small set of JavaScript-only career searches with local Chromium."""

    def __init__(self, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds
        configured = os.getenv("BROWSER_EXECUTABLE", "").strip()
        candidates = (
            configured,
            shutil.which("chromium") or "",
            shutil.which("chromium-browser") or "",
            shutil.which("google-chrome") or "",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        self.executable = next((item for item in candidates if item and Path(item).is_file()), "")
        self._render_lock = threading.Lock()

    def fetch(self, url: str) -> FetchResult:
        if not self.executable:
            raise FetchError(
                f"{url}: this source needs Chromium; set BROWSER_EXECUTABLE or use the Docker image"
            )
        command = [
            self.executable,
            "--headless=new",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--incognito",
            "--virtual-time-budget=12000",
            "--dump-dom",
            url,
        ]
        last_error: Exception | None = None
        with self._render_lock:
            for attempt in range(2):
                try:
                    completed = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        timeout=self.timeout_seconds + 20,
                        check=False,
                    )
                    html = completed.stdout.strip()
                    if completed.returncode != 0 or not html:
                        detail = completed.stderr.strip().splitlines()[-1:] or [
                            "empty browser response"
                        ]
                        raise FetchError(f"{url}: browser render failed: {detail[0]}")
                    HttpClient._check_for_block_page(html, "text/html")
                    return FetchResult(
                        requested_url=url,
                        final_url=url,
                        content_type="text/html; charset=utf-8",
                        text=html,
                    )
                except (OSError, subprocess.TimeoutExpired, FetchError) as error:
                    last_error = error
                    if attempt == 0:
                        time.sleep(1 + random.random() / 4)
        raise FetchError(f"{url}: browser render failed after retry: {last_error}") from last_error
