"""Smoke tests for `telegraph_client.publish_to_telegraph` resilience.

Three new behaviors land together here and they all need pinned tests
because the failure modes are silent — a regression turns into a 3am
"Instant View unavailable" report a week later:

  1. **edit-in-place**: when `edit_path` is provided we call `edit_page`
     first so the public URL stays stable across `/refresh` cycles.
  2. **fallback to create**: if `edit_page` raises (deleted page, auth
     drift, content-too-large), we fall through to `create_page` so a
     missing/broken edit-target never blocks the new publish.
  3. **transient retry**: one retry with 1s backoff on
     `requests.exceptions.ConnectionError` / `Timeout` — the failure mode
     that poisoned INTU's cache slot.

The SDK is monkeypatched (no network) by swapping `_telegraph_client()`
for a stub recorder. `asyncio.sleep` is also monkeypatched in the retry
path so the suite stays fast.

Run with: .venv/bin/python3 scripts/smoke_telegraph.py
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path


PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import tg_bot.telegraph_client as tc  # noqa: E402


class _StubClient:
    """Recorder + scripted-response stand-in for `telegraph.Telegraph`.

    `edit_responses` / `create_responses` are popped per call; an
    Exception instance is raised instead of returned. `create_page`
    return values default to `{"path": "<new-path-N>"}` so the URL the
    caller assembles is deterministic.
    """

    def __init__(
        self,
        edit_responses: list | None = None,
        create_responses: list | None = None,
    ) -> None:
        self.edit_calls: list[dict] = []
        self.create_calls: list[dict] = []
        self._edit = list(edit_responses or [])
        self._create = list(create_responses or [])

    def edit_page(self, **kwargs) -> dict:
        self.edit_calls.append(kwargs)
        if not self._edit:
            return {"path": kwargs["path"]}
        r = self._edit.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def create_page(self, **kwargs) -> dict:
        self.create_calls.append(kwargs)
        if not self._create:
            return {"path": f"new-path-{len(self.create_calls)}"}
        r = self._create.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _install(stub: _StubClient) -> None:
    tc._telegraph_client = lambda: stub  # type: ignore[assignment]


# ---------- _path_from_telegraph_url ----------


def test_path_extraction_canonical() -> None:
    assert tc._path_from_telegraph_url("https://telegra.ph/NVDA-Analysis-05-10") == (
        "NVDA-Analysis-05-10"
    )


def test_path_extraction_handles_none_and_empty() -> None:
    assert tc._path_from_telegraph_url("") is None
    assert tc._path_from_telegraph_url(None) is None  # type: ignore[arg-type]


def test_path_extraction_rejects_non_telegraph_urls() -> None:
    """Junk in `edit_page(path=...)` would 404; safer to fall back to
    create_page than to feed garbage to the SDK."""
    assert tc._path_from_telegraph_url("https://example.com/foo") is None
    assert tc._path_from_telegraph_url("not a url at all") is None


# ---------- publish_to_telegraph: edit_path branch ----------


async def test_edit_path_calls_edit_page_first() -> None:
    """When `edit_path` is provided + edit_page succeeds, we get the
    same `https://telegra.ph/<path>` URL back — the whole point of
    edit-in-place is that the public URL stays stable."""
    stub = _StubClient()
    _install(stub)
    url = await tc.publish_to_telegraph(
        "T Analysis", "<p>x</p>", edit_path="T-Analysis-05-10"
    )
    assert url == "https://telegra.ph/T-Analysis-05-10", url
    assert len(stub.edit_calls) == 1
    assert stub.edit_calls[0]["path"] == "T-Analysis-05-10"
    assert stub.edit_calls[0]["title"] == "T Analysis"
    assert len(stub.create_calls) == 0


async def test_edit_failure_falls_back_to_create() -> None:
    """If edit_page raises (deleted target page, auth drift, oversized
    content) we fall through to create_page so the refresh isn't blocked
    on a broken edit-target. The user gets a fresh URL — acceptable
    degradation."""
    stub = _StubClient(
        edit_responses=[RuntimeError("page not found")],
        create_responses=[{"path": "T-Analysis-05-10-2"}],
    )
    _install(stub)
    url = await tc.publish_to_telegraph(
        "T Analysis", "<p>x</p>", edit_path="T-Analysis-05-10"
    )
    assert url == "https://telegra.ph/T-Analysis-05-10-2", url
    assert len(stub.edit_calls) == 1
    assert len(stub.create_calls) == 1


async def test_edit_transient_error_retries_then_succeeds() -> None:
    """Edit-page transient errors get the same one-retry treatment as
    create. The earlier revision only retried `create_page` — a TCP
    reset during `edit_page` would log a warning, fall through to
    `create_page`, and silently mint a NEW URL, defeating the whole
    edit-in-place fix. This scenario pins that fix."""
    if not tc._TRANSIENT_NET_ERRORS:
        return
    transient_cls = tc._TRANSIENT_NET_ERRORS[0]
    stub = _StubClient(
        edit_responses=[transient_cls("conn reset"), {"path": "T-Analysis-05-10"}]
    )
    _install(stub)
    orig_sleep = asyncio.sleep
    asyncio.sleep = lambda s: orig_sleep(0)  # type: ignore[assignment]
    try:
        url = await tc.publish_to_telegraph(
            "T Analysis", "<p>x</p>", edit_path="T-Analysis-05-10"
        )
    finally:
        asyncio.sleep = orig_sleep  # type: ignore[assignment]
    # URL stays stable through the retry; no create_page involvement.
    assert url == "https://telegra.ph/T-Analysis-05-10", url
    assert len(stub.edit_calls) == 2
    assert len(stub.create_calls) == 0


async def test_edit_transient_error_exhausted_returns_none() -> None:
    """If `edit_page` keeps hitting transient errors past the retry
    budget, we MUST return None — falling through to `create_page` at
    this point would generate a new URL and undo edit-in-place. The
    caller's cache-hygiene gate will then skip the store so the next
    tap retries the edit cleanly."""
    if not tc._TRANSIENT_NET_ERRORS:
        return
    transient_cls = tc._TRANSIENT_NET_ERRORS[0]
    stub = _StubClient(
        edit_responses=[transient_cls("err1"), transient_cls("err2")],
        # No create_responses scripted — if the code falls through
        # to create_page (the bug we're guarding against), the stub
        # returns its default `{"path": "new-path-1"}` and the URL
        # assertion below catches that regression.
    )
    _install(stub)
    orig_sleep = asyncio.sleep
    asyncio.sleep = lambda s: orig_sleep(0)  # type: ignore[assignment]
    try:
        url = await tc.publish_to_telegraph(
            "T Analysis", "<p>x</p>", edit_path="T-Analysis-05-10"
        )
    finally:
        asyncio.sleep = orig_sleep  # type: ignore[assignment]
    assert url is None, f"expected None on exhausted edit retries, got {url}"
    assert len(stub.edit_calls) == 2
    assert len(stub.create_calls) == 0, (
        "transient edit failure must NOT fall through to create_page — "
        "that would leak a new URL and defeat edit-in-place"
    )


async def test_no_edit_path_goes_directly_to_create() -> None:
    """Fresh runs (no prior cached URL) skip edit_page entirely."""
    stub = _StubClient(create_responses=[{"path": "T-Analysis-05-10"}])
    _install(stub)
    url = await tc.publish_to_telegraph("T Analysis", "<p>x</p>")
    assert url == "https://telegra.ph/T-Analysis-05-10", url
    assert len(stub.edit_calls) == 0
    assert len(stub.create_calls) == 1


# ---------- publish_to_telegraph: retry semantics ----------


async def test_transient_network_error_retries_once_and_succeeds() -> None:
    """Telegraph's API has occasional flakiness (TCP resets, slow CDN
    edges). One retry with 1s backoff is the diff between a stable
    Telegraph link and a poisoned cache entry."""
    if not tc._TRANSIENT_NET_ERRORS:
        return  # `requests` not importable — skip silently.
    transient_cls = tc._TRANSIENT_NET_ERRORS[0]
    stub = _StubClient(
        create_responses=[transient_cls("conn reset"), {"path": "T-Analysis-05-10"}]
    )
    _install(stub)
    # Mock sleep to keep the suite fast — we don't want a real 1s wait.
    sleeps: list[float] = []
    orig_sleep = asyncio.sleep
    asyncio.sleep = lambda s: sleeps.append(s) or orig_sleep(0)  # type: ignore[assignment]
    try:
        url = await tc.publish_to_telegraph("T Analysis", "<p>x</p>")
    finally:
        asyncio.sleep = orig_sleep  # type: ignore[assignment]
    assert url == "https://telegra.ph/T-Analysis-05-10", url
    assert len(stub.create_calls) == 2
    assert sleeps == [1.0], sleeps


async def test_transient_network_error_exhausts_retries_returns_none() -> None:
    """Two transient failures → return None (caller's contract: None
    means publish failed; cache-hygiene gate will skip the store)."""
    if not tc._TRANSIENT_NET_ERRORS:
        return
    transient_cls = tc._TRANSIENT_NET_ERRORS[0]
    stub = _StubClient(create_responses=[transient_cls("err1"), transient_cls("err2")])
    _install(stub)
    orig_sleep = asyncio.sleep
    asyncio.sleep = lambda s: orig_sleep(0)  # type: ignore[assignment]
    try:
        url = await tc.publish_to_telegraph("T Analysis", "<p>x</p>")
    finally:
        asyncio.sleep = orig_sleep  # type: ignore[assignment]
    assert url is None, url
    assert len(stub.create_calls) == 2


async def test_non_transient_error_returns_none_without_retry() -> None:
    """API-level errors (auth, content rejection) shouldn't be retried —
    the second attempt would fail identically and we'd just slow the user
    down for 1s for nothing."""
    stub = _StubClient(create_responses=[RuntimeError("CONTENT_TOO_BIG")])
    _install(stub)
    url = await tc.publish_to_telegraph("T Analysis", "<p>x</p>")
    assert url is None, url
    assert len(stub.create_calls) == 1


# ---------- ordering ----------


SCENARIOS = [
    ("path extraction (canonical)", test_path_extraction_canonical),
    ("path extraction (None/empty)", test_path_extraction_handles_none_and_empty),
    (
        "path extraction rejects non-telegraph urls",
        test_path_extraction_rejects_non_telegraph_urls,
    ),
    ("edit_path → edit_page first (URL stable)", test_edit_path_calls_edit_page_first),
    (
        "edit_page non-transient failure → create_page fallback",
        test_edit_failure_falls_back_to_create,
    ),
    (
        "edit_page transient error → retry → success (URL stable)",
        test_edit_transient_error_retries_then_succeeds,
    ),
    (
        "edit_page transient exhausted → None (NOT create_page fallback)",
        test_edit_transient_error_exhausted_returns_none,
    ),
    ("no edit_path → create_page directly", test_no_edit_path_goes_directly_to_create),
    (
        "transient error → one retry → success",
        test_transient_network_error_retries_once_and_succeeds,
    ),
    (
        "transient error → retries exhausted → None",
        test_transient_network_error_exhausts_retries_returns_none,
    ),
    (
        "non-transient error → no retry → None",
        test_non_transient_error_returns_none_without_retry,
    ),
]


async def main() -> int:
    failures = 0
    for label, fn in SCENARIOS:
        try:
            if inspect.iscoroutinefunction(fn):
                await fn()
            else:
                fn()
        except AssertionError as e:
            failures += 1
            print(f"  {FAIL} {label}: {e}")
        except Exception as e:
            failures += 1
            print(f"  {FAIL} {label}: {type(e).__name__}: {e}")
        else:
            print(f"  {PASS} {label}")
    print()
    if failures:
        print(f"{FAIL} {failures} of {len(SCENARIOS)} failed")
        return 1
    print(f"{PASS} all {len(SCENARIOS)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
