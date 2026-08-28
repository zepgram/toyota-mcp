from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TOYOTA_", env_file=".env", extra="ignore")

    username: str
    password: SecretStr
    vin: str | None = None
    brand: Literal["T", "L"] = "T"
    use_metric: bool = True

    @field_validator("username")
    @classmethod
    def username_must_be_an_email(cls, value: str) -> str:
        if "@" not in value:
            raise ValueError("must be the MyToyota account email address")
        return value
