from datetime import datetime, timedelta, timezone

from toyota_mcp.cache import Snapshot, SnapshotCache


def _age(cache: SnapshotCache, key: str, seconds: float) -> None:
    snapshot = cache.any(key)
    assert snapshot is not None
    cache._entries[key] = Snapshot(
        value=snapshot.value,
        fetched_at=datetime.now(timezone.utc) - timedelta(seconds=seconds),
    )


def test_fresh_within_ttl() -> None:
    cache = SnapshotCache()
    cache.store("key", "value")
    snapshot = cache.fresh("key", ttl=300)
    assert snapshot is not None
    assert snapshot.value == "value"


def test_fresh_expired_returns_none_but_any_survives() -> None:
    cache = SnapshotCache()
    cache.store("key", "value")
    _age(cache, "key", 301)
    assert cache.fresh("key", ttl=300) is None
    survivor = cache.any("key")
    assert survivor is not None
    assert survivor.value == "value"


def test_fresh_missing_key() -> None:
    assert SnapshotCache().fresh("absent", ttl=300) is None
    assert SnapshotCache().any("absent") is None


def test_store_overwrites() -> None:
    cache = SnapshotCache()
    cache.store("key", "old")
    cache.store("key", "new")
    snapshot = cache.fresh("key", ttl=300)
    assert snapshot is not None
    assert snapshot.value == "new"


def test_clear() -> None:
    cache = SnapshotCache()
    cache.store("key", "value")
    cache.clear()
    assert cache.any("key") is None


def test_age_seconds() -> None:
    fetched = datetime.now(timezone.utc) - timedelta(seconds=120)
    snapshot = Snapshot(value=None, fetched_at=fetched)
    assert 119 < snapshot.age_seconds() < 122
