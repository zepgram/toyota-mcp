import httpx
import pytest

from tests.conftest import BAN_LABEL, OpenDataStub
from toyota_mcp.opendata import FrenchOpenData, OpenDataUnavailable


async def test_reverse_geocode_caches_by_rounded_position(
    opendata: FrenchOpenData, opendata_stub: OpenDataStub
) -> None:
    assert await opendata.reverse_geocode(43.60451, 1.44402) == BAN_LABEL
    assert await opendata.reverse_geocode(43.60454, 1.44398) == BAN_LABEL
    assert len(opendata_stub.requests) == 1
    assert opendata_stub.requests[0].url.host == "api-adresse.data.gouv.fr"


async def test_reverse_geocode_fails_open(
    opendata: FrenchOpenData, opendata_stub: OpenDataStub
) -> None:
    opendata_stub.status_code = 503
    assert await opendata.reverse_geocode(47.9, 1.9) is None


async def test_fuel_stations_sorted_with_distance(
    opendata: FrenchOpenData, opendata_stub: OpenDataStub
) -> None:
    stations = await opendata.fuel_stations(52.169516, 0.135654, 10, "e10", 5)
    assert [station.city for station in stations] == ["Blagnac", "Toulouse"]
    assert stations[0].distance_km < stations[1].distance_km
    assert stations[0].google_maps_url.startswith("https://www.google.com/maps?q=52.17,0.14")
    query = str(opendata_stub.requests[0].url)
    assert "within_distance" in query and "e10_prix" in query


async def test_fuel_stations_unavailable(
    opendata: FrenchOpenData, opendata_stub: OpenDataStub
) -> None:
    opendata_stub.status_code = 500
    with pytest.raises(OpenDataUnavailable):
        await opendata.fuel_stations(52.0, 0.1, 5, "gazole", 3)


async def test_fuel_station_without_price_or_position_is_skipped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"adresse": "x", "e10_prix": None, "geom": {"lat": 1.0, "lon": 1.0}},
                    {"adresse": "y", "e10_prix": 1.9, "geom": None},
                ]
            },
        )

    opendata = FrenchOpenData(transport=httpx.MockTransport(handler))
    assert await opendata.fuel_stations(1.0, 1.0, 5, "e10", 5) == []
