from dataclasses import dataclass, field
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from toyota_mcp.places import Places

Addresses = Literal["off", "osm", "fr"]


class Settings(BaseSettings):
    """Account configuration, from TOYOTA_* environment variables or a .env file."""

    model_config = SettingsConfigDict(env_prefix="TOYOTA_", env_file=".env", extra="ignore")

    username: str | None = None
    password: SecretStr | None = None
    vin: str | None = None
    brand: Literal["T", "L"] = "T"
    use_metric: bool = True

    @field_validator("username")
    @classmethod
    def username_must_be_an_email(cls, value: str | None) -> str | None:
        if value is not None and "@" not in value:
            raise ValueError("must be the MyToyota account email address")
        return value


@dataclass(frozen=True)
class ServerOptions:
    """Feature options, from the command line."""

    read_only: bool = False
    addresses: Addresses = "off"
    places: Places = field(default_factory=Places)
