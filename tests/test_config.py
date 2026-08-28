import pytest
from pydantic import ValidationError

from toyota_mcp.config import Settings


def test_explicit_values() -> None:
    settings = Settings(username="a@b.c", password="pw", vin="VIN123", brand="L", use_metric=False)
    assert settings.username == "a@b.c"
    assert settings.password.get_secret_value() == "pw"
    assert settings.vin == "VIN123"
    assert settings.brand == "L"
    assert settings.use_metric is False


def test_env_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("TOYOTA_VIN", "TOYOTA_BRAND", "TOYOTA_USE_METRIC"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("TOYOTA_USERNAME", "env@example.com")
    monkeypatch.setenv("TOYOTA_PASSWORD", "env-secret")
    settings = Settings(_env_file=None)
    assert settings.username == "env@example.com"
    assert settings.password.get_secret_value() == "env-secret"
    assert settings.vin is None
    assert settings.brand == "T"
    assert settings.use_metric is True


def test_username_must_contain_at() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(username="not-an-email", password="pw")
    assert "email address" in str(excinfo.value)


def test_password_never_leaks_in_repr() -> None:
    settings = Settings(username="a@b.c", password="hunter2")
    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings)


def test_missing_credentials_name_the_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("TOYOTA_USERNAME", "TOYOTA_PASSWORD"):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    missing = {error["loc"][0] for error in excinfo.value.errors()}
    assert missing == {"username", "password"}
