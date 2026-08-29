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

REMOTE_COMMAND_CODES = {
    "CTP-REMOTE-40006": (
        "Toyota's backend does not know that remote command (CTP-REMOTE-40006) — "
        "nothing was sent to the car."
    ),
    "CTP-REMOTE-40041": (
        "This vehicle does not support that remote command (CTP-REMOTE-40041) — "
        "nothing was sent to the car."
    ),
}

_API_ERROR_STATUS = re.compile(r"\b(\d{3})\b")
_RESPONSE_CODE = re.compile(r'"responseCode"\s*:\s*"(CTP-[A-Z]+-\d+)"')


def vin_not_found(requested_vin: str, available: list[str]) -> ToolError:
    return ToolError(
        f"VIN ending …{requested_vin[-4:]} not found on this account. "
        f"Available vehicles: {', '.join(available) or 'none'}."
    )


def request_rejected(status: int) -> ToolError:
    return ToolError(
        f"Toyota rejected the request (HTTP {status}). If this persists, re-check "
        "TOYOTA_USERNAME / TOYOTA_PASSWORD or run `uvx toyota-mcp doctor`."
    )


def api_status_code(exc: ToyotaApiError) -> int | None:
    match = _API_ERROR_STATUS.search(str(exc))
    return int(match.group(1)) if match else None


def is_transient(exc: Exception) -> bool:
    if isinstance(exc, ToyotaApiError):
        return _is_transient_status(api_status_code(exc))
    return isinstance(exc, httpx.TimeoutException | httpx.TransportError)


def _is_transient_status(status: int | None) -> bool:
    return status is None or status == 429 or status >= 500


def translate(exc: Exception) -> ToolError:
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, ToyotaInvalidUsernameError):
        return ToolError(INVALID_USERNAME)
    if isinstance(exc, ToyotaLoginError):
        return ToolError(LOGIN_FAILED)
    if isinstance(exc, ToyotaApiError):
        code = _RESPONSE_CODE.search(str(exc))
        if code is not None and code.group(1) in REMOTE_COMMAND_CODES:
            return ToolError(REMOTE_COMMAND_CODES[code.group(1)])
        status = api_status_code(exc)
        if status in (403, 404):
            return ToolError(ENDPOINT_CHANGED)
        if status is None or _is_transient_status(status):
            return ToolError(API_UNAVAILABLE)
        return request_rejected(status)
    if isinstance(exc, ToyotaInternalError | ValidationError):
        return ToolError(UNEXPECTED_PAYLOAD)
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return ToolError(NETWORK_ERROR)
    return ToolError(f"Unexpected error ({type(exc).__name__}) — details are in the server logs.")
