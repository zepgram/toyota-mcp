from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from toyota_mcp.geo import haversine_km

logger = logging.getLogger(__name__)

Provider = Literal["fr", "osm"]
BAN_REVERSE_URL = "https://api-adresse.data.gouv.fr/reverse/"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
FUEL_PRICES_URL = (
    "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/"
    "prix-des-carburants-en-france-flux-instantane-v2/records"
)
FUEL_PRICES_SOURCE = (
    "Prices are self-reported by the stations to the French government open-data feed "
    "(data.economie.gouv.fr, live flow)."
)
FUEL_PRICES_FRANCE_ONLY = (
    "Fuel prices are only available in France — set TOYOTA_OPEN_DATA=fr to enable them."
)
FuelKind = Literal["e10", "sp95", "sp98", "e85", "gazole", "gplc"]

_TIMEOUT = httpx.Timeout(5.0)
_NOMINATIM_MIN_INTERVAL = 1.0


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


class OpenData:
    def __init__(self, provider: Provider, transport: httpx.AsyncBaseTransport | None = None):
        self.provider = provider
        self._client = httpx.AsyncClient(
            timeout=_TIMEOUT, transport=transport, headers={"User-Agent": "toyota-mcp"}
        )
        self._addresses: dict[tuple[float, float], str | None] = {}
        self._nominatim_lock = asyncio.Lock()
        self._nominatim_last_call = 0.0

    async def reverse_geocode(self, latitude: float, longitude: float) -> str | None:
        key = (round(latitude, 4), round(longitude, 4))
        if key in self._addresses:
            return self._addresses[key]
        try:
            label = (
                await self._ban_reverse(*key)
                if self.provider == "fr"
                else await self._nominatim_reverse(*key)
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.warning("reverse geocoding failed (%s)", type(exc).__name__)
            return None
        self._addresses[key] = label
        return label

    async def fuel_stations(
        self, latitude: float, longitude: float, radius_km: int, fuel: FuelKind, limit: int
    ) -> list[FuelStation]:
        if self.provider != "fr":
            raise OpenDataUnavailable(FUEL_PRICES_FRANCE_ONLY)
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

    async def _ban_reverse(self, latitude: float, longitude: float) -> str | None:
        response = await self._client.get(
            BAN_REVERSE_URL, params={"lat": latitude, "lon": longitude}
        )
        response.raise_for_status()
        features = response.json().get("features") or []
        return str(features[0]["properties"]["label"]) if features else None

    async def _nominatim_reverse(self, latitude: float, longitude: float) -> str | None:
        # Nominatim's usage policy: at most one request per second.
        async with self._nominatim_lock:
            await asyncio.sleep(
                max(0.0, _NOMINATIM_MIN_INTERVAL - (time.monotonic() - self._nominatim_last_call))
            )
            self._nominatim_last_call = time.monotonic()
            response = await self._client.get(
                NOMINATIM_REVERSE_URL,
                params={"lat": latitude, "lon": longitude, "format": "jsonv2"},
            )
        response.raise_for_status()
        name = response.json().get("display_name")
        return str(name) if name else None


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
            haversine_km(latitude, longitude, float(station_latitude), float(station_longitude)), 1
        ),
        open_24h=record.get("horaires_automate_24_24") == "Oui",
        available_fuels=list(record.get("carburants_disponibles") or []),
        latitude=float(station_latitude),
        longitude=float(station_longitude),
        google_maps_url=f"https://www.google.com/maps?q={station_latitude},{station_longitude}",
    )
