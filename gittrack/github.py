"""GitHub API client: token resolution, rate-limit discipline, retries, discovery.

Two rate-limit buckets matter here and they are independent:
  * core   - 5000 req/hr authenticated, 60 unauthenticated (the /repos endpoints)
  * search - 30 req/min authenticated, 10 unauthenticated (/search/repositories)

Discovery and the top-N snapshot both ride the *search* endpoint, because it
returns 100 complete repo objects per request. Snapshotting the whole top 100
therefore costs a single API call.
"""

from __future__ import annotations

import contextlib
import logging
import os
import random
import subprocess
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("gittrack.github")

USER_AGENT = "gittrack/0.1 (+https://github.com)"
API_VERSION = "2022-11-28"


class GitHubError(RuntimeError):
    pass


class RateLimitExceeded(GitHubError):
    """Raised when waiting for the reset would exceed max_wait."""


def resolve_token(token_env: str = "GITHUB_TOKEN", use_gh_cli: bool = True) -> str | None:
    """Env var first, then `gh auth token`. Never read from the config file."""
    tok = os.environ.get(token_env) or os.environ.get("GH_TOKEN")
    if tok:
        return tok.strip()
    if use_gh_cli:
        try:
            out = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return None


@dataclass
class Bucket:
    limit: int | None = None
    remaining: int | None = None
    reset: int | None = None


@dataclass
class GitHubClient:
    token: str | None = None
    api_base: str = "https://api.github.com"
    max_wait: float = 900.0
    max_retries: int = 5
    timeout: float = 30.0

    requests_made: int = field(default=0, init=False)
    buckets: dict[str, Bucket] = field(default_factory=dict, init=False)
    _client: httpx.Client | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._client = httpx.Client(
            base_url=self.api_base, headers=headers, timeout=self.timeout,
            follow_redirects=True,
        )

    # ------------------------------------------------------------------ plumbing

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    def _note_headers(self, resp: httpx.Response) -> None:
        res = resp.headers.get("x-ratelimit-resource", "core")
        b = self.buckets.setdefault(res, Bucket())
        for attr, hdr in (("limit", "x-ratelimit-limit"),
                          ("remaining", "x-ratelimit-remaining"),
                          ("reset", "x-ratelimit-reset")):
            if hdr in resp.headers:
                # A malformed rate-limit header is not worth failing a request over.
                with contextlib.suppress(ValueError):
                    setattr(b, attr, int(resp.headers[hdr]))

    def _wait_if_exhausted(self, resource: str) -> None:
        b = self.buckets.get(resource)
        if not b or b.remaining is None or b.remaining > 0 or b.reset is None:
            return
        delay = b.reset - time.time() + 1.0
        if delay <= 0:
            return
        if delay > self.max_wait:
            raise RateLimitExceeded(
                f"{resource} rate limit exhausted; resets in {delay:.0f}s "
                f"(max_wait={self.max_wait:.0f}s). "
                + ("Try again later." if self.token else
                   "Set GITHUB_TOKEN or run `gh auth login` for a 5000/hr limit.")
            )
        log.info("rate limit for %s exhausted, sleeping %.0fs", resource, delay)
        time.sleep(delay)

    def request(self, method: str, path: str, *, resource: str = "core",
                headers: dict | None = None, **kw) -> httpx.Response:
        assert self._client is not None
        self._wait_if_exhausted(resource)
        last_exc: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                resp = self._client.request(method, path, headers=headers, **kw)
            except httpx.TransportError as exc:  # DNS, connect, read timeouts
                last_exc = exc
                backoff = min(60.0, 2.0 ** attempt) + random.uniform(0, 1)
                log.warning("transport error (%s), retrying in %.1fs", exc, backoff)
                time.sleep(backoff)
                continue

            self.requests_made += 1
            self._note_headers(resp)

            if resp.status_code < 400 or resp.status_code == 304:
                return resp

            # Explicit throttling signals.
            if resp.status_code in (403, 429):
                retry_after = resp.headers.get("retry-after")
                body = (resp.text or "")[:400].lower()
                if retry_after:
                    delay = float(retry_after)
                elif "secondary rate limit" in body:
                    delay = 60.0 * (attempt + 1)
                elif resp.headers.get("x-ratelimit-remaining") == "0":
                    reset = int(resp.headers.get("x-ratelimit-reset", "0"))
                    delay = max(0.0, reset - time.time() + 1.0)
                else:
                    raise GitHubError(f"{resp.status_code} {method} {path}: {resp.text[:300]}")
                if delay > self.max_wait:
                    raise RateLimitExceeded(
                        f"throttled on {path}; would need to wait {delay:.0f}s"
                    )
                log.info("throttled on %s, sleeping %.0fs", path, delay)
                time.sleep(delay + random.uniform(0, 1))
                continue

            if resp.status_code >= 500:
                backoff = min(60.0, 2.0 ** attempt) + random.uniform(0, 1)
                log.warning("server error %s on %s, retrying in %.1fs",
                            resp.status_code, path, backoff)
                time.sleep(backoff)
                continue

            raise GitHubError(f"{resp.status_code} {method} {path}: {resp.text[:300]}")

        raise GitHubError(f"giving up on {method} {path} after {self.max_retries} attempts: {last_exc}")

    # --------------------------------------------------------------- endpoints

    def rate_limit(self) -> dict:
        resp = self.request("GET", "/rate_limit")
        return resp.json().get("resources", {})

    def get_repo(self, full_name: str, etag: str | None = None) -> tuple[dict | None, str | None]:
        """Return (repo, etag). repo is None on 304 Not Modified (nothing changed).

        304 responses do not count against the REST rate limit, so passing the
        stored etag makes re-polling an idle repo effectively free.
        """
        headers = {"If-None-Match": etag} if etag else None
        resp = self.request("GET", f"/repos/{full_name}", headers=headers)
        if resp.status_code == 304:
            return None, etag
        return resp.json(), resp.headers.get("etag")

    def search_repositories(self, q: str, page: int = 1, per_page: int = 100,
                            sort: str = "stars", order: str = "desc") -> dict:
        resp = self.request(
            "GET", "/search/repositories", resource="search",
            params={"q": q, "sort": sort, "order": order,
                    "per_page": per_page, "page": page},
        )
        return resp.json()

    def top_repositories(self, top_n: int, min_stars: int = 50,
                         extra_qualifiers: str = "") -> list[dict]:
        """The top `top_n` repos by stars, as full repo objects.

        The search API hard-caps any single query at 1000 results, so beyond that
        we slice the star axis: once a query is exhausted we re-query with an
        upper bound at the lowest star count seen. Ties across the boundary are
        handled by de-duplicating on repo id rather than by trying to page past them.
        """
        seen: dict[int, dict] = {}
        upper: int | None = None
        # Guard against pathological ties (>1000 repos on the same star count).
        for _slice in range(64):
            if len(seen) >= top_n:
                break
            bound = f"{min_stars}..{upper}" if upper is not None else f">={min_stars}"
            q = f"stars:{bound}"
            if extra_qualifiers:
                q += " " + extra_qualifiers
            before_slice = len(seen)
            lowest = upper

            for page in range(1, 11):  # 10 pages x 100 = the 1000-result cap
                if len(seen) >= top_n:
                    break
                data = self.search_repositories(q, page=page)
                items = data.get("items") or []
                if not items:
                    break
                for item in items:
                    seen.setdefault(item["id"], item)
                    lowest = item["stargazers_count"] if lowest is None else min(
                        lowest, item["stargazers_count"])
                if len(items) < 100:
                    break

            if len(seen) == before_slice:
                log.info("discovery slice produced no new repos; stopping at %d", len(seen))
                break
            if lowest is None or lowest <= min_stars:
                break
            # Next slice starts at the lowest star count we saw (inclusive, so ties
            # at the boundary are re-fetched and de-duplicated rather than dropped).
            if upper is not None and lowest >= upper:
                break
            upper = lowest

        ranked = sorted(seen.values(), key=lambda r: r["stargazers_count"], reverse=True)
        return ranked[:top_n]

    def iter_stargazers(self, full_name: str, max_pages: int = 400):
        """Yield ISO timestamps of individual stars, newest-last.

        Used only by the opt-in `backfill` command. GitHub caps pagination at
        400 pages x 100 = 40,000 stars, so repos above that can only have their
        most recent 40k stars reconstructed.
        """
        for page in range(1, max_pages + 1):
            resp = self.request(
                "GET", f"/repos/{full_name}/stargazers",
                headers={"Accept": "application/vnd.github.star+json"},
                params={"per_page": 100, "page": page},
            )
            items = resp.json()
            if not isinstance(items, list) or not items:
                return
            for it in items:
                if isinstance(it, dict) and it.get("starred_at"):
                    yield it["starred_at"]
            if len(items) < 100:
                return
