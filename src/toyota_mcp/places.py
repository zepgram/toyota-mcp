from __future__ import annotations

from dataclasses import dataclass

from toyota_mcp.geo import haversine_km

PLACE_RADIUS_KM = 0.2
FORMAT_HINT = 'expected "name=lat,lon;name=lat,lon", e.g. "home=43.6045,1.4440;work=43.6290,1.3630"'


@dataclass(frozen=True)
class Place:
    name: str
    latitude: float
    longitude: float


class Places:
    def __init__(self, places: tuple[Place, ...] = ()) -> None:
        self._places = places

    @classmethod
    def parse(cls, spec: str) -> Places:
        places: list[Place] = []
        for entry in filter(None, (part.strip() for part in spec.split(";"))):
            name, separator, coordinates = entry.partition("=")
            latitude, comma, longitude = coordinates.partition(",")
            if not (separator and comma and name.strip()):
                raise ValueError(f"invalid place {entry!r}: {FORMAT_HINT}")
            try:
                places.append(Place(name.strip(), float(latitude), float(longitude)))
            except ValueError as exc:
                raise ValueError(f"invalid coordinates in {entry!r}: {FORMAT_HINT}") from exc
        return cls(tuple(places))

    def __bool__(self) -> bool:
        return bool(self._places)

    def match(self, latitude: float, longitude: float) -> str | None:
        nearest = min(
            self._places,
            key=lambda place: haversine_km(latitude, longitude, place.latitude, place.longitude),
            default=None,
        )
        if nearest is None:
            return None
        distance = haversine_km(latitude, longitude, nearest.latitude, nearest.longitude)
        return nearest.name if distance <= PLACE_RADIUS_KM else None
