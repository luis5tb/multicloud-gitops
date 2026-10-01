import asyncio

import httpx
import pytest

from acme_agent.auth import OLS_CLUSTER_HEADER, add_cluster_header
from acme_agent.cluster_context import (
    ClusterIdMissingError,
    cluster_id_scope,
    get_cluster_id,
)


def test_get_cluster_id_defaults_to_none():
    assert get_cluster_id() is None


def test_cluster_id_scope_binds_and_restores():
    assert get_cluster_id() is None
    with cluster_id_scope("cluster-a"):
        assert get_cluster_id() == "cluster-a"
    assert get_cluster_id() is None


def test_cluster_id_scope_restores_on_exception():
    with pytest.raises(RuntimeError):
        with cluster_id_scope("cluster-a"):
            assert get_cluster_id() == "cluster-a"
            raise RuntimeError("boom")
    assert get_cluster_id() is None


def test_nested_scopes_compose():
    with cluster_id_scope("outer"):
        with cluster_id_scope("inner"):
            assert get_cluster_id() == "inner"
        assert get_cluster_id() == "outer"
    assert get_cluster_id() is None


def test_add_cluster_header_attaches_header_for_post():
    async def run() -> httpx.Request:
        with cluster_id_scope("cluster-a"):
            request = httpx.Request("POST", "https://example.test/rpc")
            await add_cluster_header(request)
        return request

    request = asyncio.run(run())
    assert request.headers.get(OLS_CLUSTER_HEADER) == "cluster-a"


def test_add_cluster_header_absent_for_get_card_fetch():
    async def run() -> httpx.Request:
        with cluster_id_scope("cluster-a"):
            request = httpx.Request(
                "GET", "https://example.test/.well-known/agent-card.json"
            )
            await add_cluster_header(request)
        return request

    request = asyncio.run(run())
    assert OLS_CLUSTER_HEADER not in request.headers


def test_add_cluster_header_fails_closed_without_bound_id():
    async def run() -> None:
        request = httpx.Request("POST", "https://example.test/rpc")
        await add_cluster_header(request)

    with pytest.raises(ClusterIdMissingError):
        asyncio.run(run())


def test_concurrent_tasks_do_not_leak_cluster_id():
    """Two 'invocations' running concurrently in separate asyncio Tasks must
    never observe each other's bound cluster id -- the core isolation
    property contextvars.ContextVar gives us (vs. a process-global header).
    """

    results: dict[str, str | None] = {}

    async def invocation(
        name: str, cluster_id: str, *, delay_before: float, delay_after: float
    ) -> None:
        with cluster_id_scope(cluster_id):
            await asyncio.sleep(delay_before)
            # Simulate interleaving: by now the other invocation's task has
            # had a chance to run while this one was suspended.
            results[name] = get_cluster_id()
            await asyncio.sleep(delay_after)
            # Still correct after yielding again.
            results[f"{name}-after"] = get_cluster_id()

    async def run() -> None:
        await asyncio.gather(
            invocation("a", "cluster-a", delay_before=0.01, delay_after=0.02),
            invocation("b", "cluster-b", delay_before=0.02, delay_after=0.01),
        )

    asyncio.run(run())

    assert results["a"] == "cluster-a"
    assert results["b"] == "cluster-b"
    assert results["a-after"] == "cluster-a"
    assert results["b-after"] == "cluster-b"
    assert get_cluster_id() is None
