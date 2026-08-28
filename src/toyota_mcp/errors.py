import re

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError
from pytoyoda.exceptions import (
    ToyotaApiError,
    ToyotaInternalError,
    ToyotaInvalidUsernameError,
    ToyotaLoginError,
)

LOGIN_FAILED = (
    "MyToyota sign-in failed. Check TOYOTA_USERNAME and TOYOTA_PASSWORD. "
    "Accounts with MFA/2FA enabled are not supported by the MyToyota API. "
    "Login is paused for 60 seconds — run `uvx toyota-mcp doctor` in a terminal "
    "for a full diagnosis."
)
INVALID_USERNAME = "TOYOTA_USERNAME must be the MyToyota account email address."
API_UNAVAILABLE = (
    "Toyota's vehicle API is rate-limiting or temporarily unavailable "
    "(transient — NOT an authentication problem). The car only uploads new data "
    "when parked — ask again in a minute or two."
)
ENDPOINT_CHANGED = (
    "Toyota appears to have changed this API endpoint. Check for a newer "
    "toyota-mcp or pytoyoda release (https://github.com/pytoyoda/pytoyoda/issues)."
)
UNEXPECTED_PAYLOAD = (
    "Toyota returned data in an unexpected format — pytoyoda may need an update. "
    "Details are in the server logs."
)
NETWORK_ERROR = "Network error reaching Toyota's cloud — check connectivity and retry."
NO_VEHICLES = (
    "No vehicles are attached to this MyToyota account. Verify the vehicle appears "
    "in the MyToyota mobile app, or run `uvx toyota-mcp doctor`."
)
LOCATION_NEVER_REPORTED = "No parked location has been reported by this vehicle yet."

_API_ERROR_STATUS = re.compile(r"\b(\d{3})\b")


def vin_not_found(requested_vin: str, available: list[str]) -> ToolError:
    return ToolError(
        f"VIN ending …{requested_vin[-4:]} not found on this account. "
        f"Available vehicles: {', '.join(available) or 'none'}."
    )


def api_status_code(exc: ToyotaApiError) -> int | None:
    match = _API_ERROR_STATUS.search(str(exc))
    return int(match.group(1)) if match else None


def is_transient(exc: Exception) -> bool:
    if isinstance(exc, ToyotaApiError):
        status = api_status_code(exc)
        return status is None or status == 429 or status >= 500
    return isinstance(exc, httpx.TimeoutException | httpx.TransportError)


def translate(exc: Exception) -> ToolError:
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, ToyotaInvalidUsernameError):
        return ToolError(INVALID_USERNAME)
    if isinstance(exc, ToyotaLoginError):
        return ToolError(LOGIN_FAILED)
    if isinstance(exc, ToyotaApiError):
        status = api_status_code(exc)
        if status in (403, 404):
            return ToolError(ENDPOINT_CHANGED)
        return ToolError(API_UNAVAILABLE)
    if isinstance(exc, ToyotaInternalError | ValidationError):
        return ToolError(UNEXPECTED_PAYLOAD)
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return ToolError(NETWORK_ERROR)
    return ToolError(f"Unexpected error ({type(exc).__name__}) — details are in the server logs.")
