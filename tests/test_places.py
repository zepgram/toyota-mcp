import pytest
from pydantic import ValidationError

from toyota_mcp.config import Settings
from toyota_mcp.places import Places


def test_parse_and_match_within_200_metres() -> None:
    places = Places.parse("home=43.6045,1.4440; work=43.6290,1.3630")
    assert places
    assert places.match(43.6047, 1.4442) == "home"
    assert places.match(43.6290, 1.3630) == "work"
    assert places.match(47.95, 1.90) is None


def test_empty_spec_matches_nothing() -> None:
    assert not Places.parse("")
    assert Places.parse("").match(1.0, 1.0) is None


@pytest.mark.parametrize("spec", ["home", "home=1", "home=a,b", "=1,2"])
def test_invalid_spec_is_rejected_with_the_expected_format(spec: str) -> None:
    with pytest.raises(ValueError, match="name=lat,lon"):
        Places.parse(spec)


def test_settings_validate_places() -> None:
    with pytest.raises(ValidationError):
        Settings(username="a@b.c", password="x", places="home=oops")
    assert Settings(username="a@b.c", password="x", places="home=1,2").places == "home=1,2"
