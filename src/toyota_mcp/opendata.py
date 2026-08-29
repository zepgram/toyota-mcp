from __future__ import annotations

import logging
from math import asin, cos, radians, sin, sqrt
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

BAN_REVERSE_URL = "https://api-adresse.data.gouv.fr/reverse/"
FUEL_PRICES_URL = (
    "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/"
    "prix-des-carburants-en-france-flux-instantane-v2/records"
)
FUEL_PRICES_SOURCE = (
    "Prix relevés par les stations (data.economie.gouv.fr, flux instantané) — "
    "prices are those reported by the stations themselves."
)
FuelKind = Literal["e10", "sp95", "sp98", "e85", "gazole", "gplc"]
FUEL_KINDS: tuple[FuelKind, ...] = ("e10", "sp95", "sp98", "e85", "gazole", "gplc")

_TIMEOUT = httpx.Timeout(5.0)
_EARTH_RADIUS_KM = 6371.0


class OpenDataUnavailable(Exception):
    pass


class FuelStation(BaseModel):
    address: str
    postcode: str | None
    city: str | None
    price_eur_per_litre: float
    price_updated_at: str | None = Field(description="ISO timestamp reported by the station.")
    distance_km: float = Field(description="Straight-line distance from the car.")
    open_24h: bool
    available_fuels: list[str]
    latitude: float
    longitude: float
    google_maps_url: str


class FrenchOpenData:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            timeout=_TIMEOUT, transport=transport, headers={"User-Agent": "toyota-mcp"}
        )
        self._addresses: dict[tuple[float, float], str | None] = {}

    async def reverse_geocode(self, latitude: float, longitude: float) -> str | None:
        key = (round(latitude, 4), round(longitude, 4))
        if key in self._addresses:
            return self._addresses[key]
        try:
            response = await self._client.get(
                BAN_REVERSE_URL, params={"lat": key[0], "lon": key[1]}
            )
            response.raise_for_status()
            features = response.json().get("features") or []
            label = str(features[0]["properties"]["label"]) if features else None
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.warning("reverse geocoding failed (%s)", type(exc).__name__)
            return None
        self._addresses[key] = label
        return label

    async def fuel_stations(
        self, latitude: float, longitude: float, radius_km: int, fuel: FuelKind, limit: int
    ) -> list[FuelStation]:
        where = (
            f"within_distance(geom, geom'POINT({longitude} {latitude})', {radius_km}km) "
            f"AND {fuel}_prix IS NOT NULL"
        )
        try:
            response = await self._client.get(
                FUEL_PRICES_URL,
                params={"where": where, "order_by": f"{fuel}_prix", "limit": limit},
            )
            response.raise_for_status()
            results = response.json()["results"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("fuel price lookup failed (%s)", type(exc).__name__)
            raise OpenDataUnavailable(
                "The French fuel-price open-data service is unavailable right now — retry later."
            ) from exc
        return [
            station
            for record in results
            if (station := _station(record, fuel, latitude, longitude)) is not None
        ]

    async def aclose(self) -> None:
        await self._client.aclose()


def _station(
    record: dict[str, Any], fuel: FuelKind, latitude: float, longitude: float
) -> FuelStation | None:
    geometry = record.get("geom") or {}
    price = record.get(f"{fuel}_prix")
    station_latitude = geometry.get("lat")
    station_longitude = geometry.get("lon")
    if price is None or station_latitude is None or station_longitude is None:
        return None
    return FuelStation(
        address=str(record.get("adresse") or "").strip(),
        postcode=record.get("cp"),
        city=record.get("ville"),
        price_eur_per_litre=float(price),
        price_updated_at=record.get(f"{fuel}_maj"),
        distance_km=round(
            _haversine_km(latitude, longitude, float(station_latitude), float(station_longitude)),
            1,
        ),
        open_24h=record.get("horaires_automate_24_24") == "Oui",
        available_fuels=list(record.get("carburants_disponibles") or []),
        latitude=float(station_latitude),
        longitude=float(station_longitude),
        google_maps_url=f"https://www.google.com/maps?q={station_latitude},{station_longitude}",
    )


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lambda = radians(lon2 - lon1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * asin(sqrt(a))
