"""Bounded rate limiting preserves debt when clients rotate their keys."""

import httpx

from ocr_server import TokenBucket, create_app
from ocrserver.config import Settings
from test_mcp_schema import FakePipeline, FakeRecognizer
from test_mcp_tools import PNG


def test_refill_capacity_and_idle_eviction():
    now = [0.0]
    limiter = TokenBucket(60, 2, 2, 10, clock=lambda: now[0])
    assert limiter.allow("a") and limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")
    assert not limiter.allow("c")
    now[0] = 1
    assert limiter.allow("a")
    now[0] = 10
    assert limiter.allow("c")  # b expired, a is still active.
    assert len(limiter._buckets) == 2


def test_idle_does_not_erase_unreplenished_debt():
    now = [0.0]
    limiter = TokenBucket(1, 2, 1, 1, clock=lambda: now[0])
    assert limiter.allow("a") and limiter.allow("a")
    now[0] = 2
    assert not limiter.allow("b")
    assert not limiter.allow("a")
    now[0] = 122
    assert limiter.allow("b")


def test_ten_thousand_client_cap_does_not_evict_active_clients():
    limiter = TokenBucket(1, 1, clock=lambda: 0)
    assert all(limiter.allow(str(i)) for i in range(10000))
    assert not limiter.allow("overflow")
    assert not limiter.allow("0")
    assert len(limiter._buckets) == 10000


def test_zero_rate_disables_without_allocating_buckets():
    limiter = TokenBucket(0, 1)
    assert all(limiter.allow(str(i)) for i in range(10001))
    assert not limiter._buckets


async def test_stats_and_metrics_polling_exempt_from_rate_limit():
    app = create_app(
        Settings(rate_limit_rpm=1, rate_limit_burst=1),
        FakePipeline(),
        FakeRecognizer(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        # The console polls both every 5 s per open tab: they must never
        # drain the caller's request budget. Auth still applies.
        for _ in range(5):
            assert (await client.get("/stats")).status_code == 200
            assert (await client.get("/metrics")).status_code == 200
        body = {"image_base64": PNG}
        assert (await client.post("/ocr", json=body)).status_code == 200
        assert (await client.post("/ocr", json=body)).status_code == 429


def test_idle_bucket_with_debt_does_not_hide_expired_full_bucket():
    now = [0.0]
    limiter = TokenBucket(60, 10, 2, 1, clock=lambda: now[0])
    assert all(limiter.allow("depleted") for _ in range(10))
    now[0] = 0.1
    assert limiter.allow("full")
    now[0] = 2
    assert limiter.allow("new")
    assert "depleted" in limiter._buckets
    assert "full" not in limiter._buckets
