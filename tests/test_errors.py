import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ValidationError
from pytoyoda.exceptions import (
    ToyotaApiError,
    ToyotaInternalError,
    ToyotaInvalidUsernameError,
    ToyotaLoginError,
)

from toyota_mcp import errors


class _Strict(BaseModel):
    number: int


def _validation_error() -> ValidationError:
    with pytest.raises(ValidationError) as excinfo:
        _Strict(number="not-a-number")
    return excinfo.value


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ToyotaLoginError("Authentication Failed. 401, denied."), errors.LOGIN_FAILED),
        (ToyotaInvalidUsernameError("bad"), errors.INVALID_USERNAME),
        (ToyotaApiError("Request Failed. 429, slow down."), errors.API_UNAVAILABLE),
        (ToyotaApiError("Request Failed. 500, boom."), errors.API_UNAVAILABLE),
        (ToyotaApiError("Request Failed. 503, later."), errors.API_UNAVAILABLE),
        (ToyotaApiError("Request Failed. 403, gone."), errors.ENDPOINT_CHANGED),
        (ToyotaApiError("Request Failed. 404, nope."), errors.ENDPOINT_CHANGED),
        (ToyotaInternalError("weird"), errors.UNEXPECTED_PAYLOAD),
        (httpx.ConnectTimeout("slow"), errors.NETWORK_ERROR),
        (httpx.ConnectError("refused"), errors.NETWORK_ERROR),
    ],
)
def test_translate_table(exc: Exception, expected: str) -> None:
    assert errors.translate(exc).args[0] == expected


@pytest.mark.parametrize("status", [400, 401])
def test_translate_rejected_request_is_not_called_transient(status: int) -> None:
    message = errors.translate(ToyotaApiError(f"Request Failed. {status}, denied.")).args[0]
    assert str(status) in message
    assert "doctor" in message
    assert "NOT an authentication problem" not in message


def test_translate_validation_error() -> None:
    assert errors.translate(_validation_error()).args[0] == errors.UNEXPECTED_PAYLOAD


def test_translate_passes_tool_error_through() -> None:
    original = ToolError("already translated")
    assert errors.translate(original) is original


def test_translate_unexpected_names_the_type() -> None:
    message = errors.translate(KeyError("boom")).args[0]
    assert "KeyError" in message
    assert "server logs" in message


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (ToyotaApiError("Request Failed. 429, slow down."), True),
        (ToyotaApiError("Request Failed. 500, boom."), True),
        (ToyotaApiError("Request Failed. 403, gone."), False),
        (ToyotaApiError("Request Failed. 404, nope."), False),
        (ToyotaApiError("no status in message"), True),
        (ToyotaApiError("Request Failed. 401, expired."), False),
        (httpx.ConnectTimeout("slow"), True),
        (httpx.ConnectError("refused"), True),
        (ToyotaLoginError("Authentication Failed. 401, denied."), False),
        (KeyError("boom"), False),
    ],
)
def test_is_transient(exc: Exception, transient: bool) -> None:
    assert errors.is_transient(exc) is transient


def test_vin_not_found_lists_available() -> None:
    error = errors.vin_not_found("JTDZARBE0RJ000042", ["Corolla (…0042)"])
    message = error.args[0]
    assert "…0042" in message
    assert "Corolla" in message
