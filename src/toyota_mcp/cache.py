from dataclasses import dataclass, field
from datetime import UTC, datetime

TTL_STATUS = 300
TTL_TELEMETRY = 300
TTL_LOCATION = 300
TTL_HEALTH_BUNDLE = 900
REFRESH_FLOOR = 60
LOGIN_COOLDOWN = 60


@dataclass(frozen=True)
class Snapshot:
    value: object
    fetched_at: datetime

    def age_seconds(self, now: datetime | None = None) -> float:
        current = now or datetime.now(UTC)
        return (current - self.fetched_at).total_seconds()


@dataclass
class SnapshotCache:
    _entries: dict[str, Snapshot] = field(default_factory=dict)

    def fresh(self, key: str, ttl: float) -> Snapshot | None:
        snapshot = self._entries.get(key)
        if snapshot is None or snapshot.age_seconds() > ttl:
            return None
        return snapshot

    def any(self, key: str) -> Snapshot | None:
        return self._entries.get(key)

    def store(self, key: str, value: object) -> Snapshot:
        snapshot = Snapshot(value=value, fetched_at=datetime.now(UTC))
        self._entries[key] = snapshot
        return snapshot

    def clear(self) -> None:
        self._entries.clear()
