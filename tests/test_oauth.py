from __future__ import annotations

import base64
import copy
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from pydantic import AnyHttpUrl
from pytoyoda.exceptions import ToyotaLoginError

from toyota_mcp.gateway import VehicleGateway
from toyota_mcp.oauth import SCOPE, Grants, OwnerAuthorizationServer
from toyota_mcp.server import create_server
from toyota_mcp.session import Session

PUBLIC_URL = "http://localhost:8787"
REDIRECT_URI = "http://localhost:9999/callback"
CREDENTIALS = {"username": "driver@example.com", "password": "hunter2"}


@pytest.fixture
def grants(tmp_path: Path) -> Grants:
    return Grants.load(tmp_path / "oauth.json")


@pytest.fixture(autouse=True)
def toyota_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sign_in(username: str, password: str, brand: str = "T") -> Session:
        if password != "hunter2":
            raise ToyotaLoginError("Authentication Failed. 401, denied.")
        return Session(username=username, refresh_token="refresh-value")

    monkeypatch.setattr("toyota_mcp.server.sign_in", fake_sign_in)


@pytest.fixture
def app(gateway: VehicleGateway, grants: Grants) -> Any:
    server = create_server(
        gateway,
        authorization=OwnerAuthorizationServer(grants),
        auth_settings=AuthSettings(
            issuer_url=AnyHttpUrl(PUBLIC_URL),
            resource_server_url=AnyHttpUrl(f"{PUBLIC_URL}/mcp"),
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
        ),
    )
    return server.streamable_http_app()


@pytest.fixture
async def http(app: Any) -> Any:
    # The streamable-HTTP layer starts its task group in the app's lifespan.
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL, follow_redirects=False
        ) as client,
    ):
        yield client


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    return verifier, challenge.rstrip("=")


async def _register(http: httpx.AsyncClient) -> str:
    response = await http.post(
        "/register",
        json={
            "client_name": "Claude",
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["client_id"])


async def _authorize(http: httpx.AsyncClient, client_id: str, challenge: str) -> str:
    response = await http.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "opaque-state",
            "scope": SCOPE,
        },
    )
    assert response.status_code == 302, response.text
    return str(response.headers["location"])


async def test_the_server_advertises_where_to_get_a_token(http: httpx.AsyncClient) -> None:
    resource = await http.get("/.well-known/oauth-protected-resource/mcp")
    assert resource.status_code == 200
    assert resource.json()["resource"] == f"{PUBLIC_URL}/mcp"

    server = await http.get("/.well-known/oauth-authorization-server")
    assert server.status_code == 200
    metadata = server.json()
    assert metadata["issuer"].rstrip("/") == PUBLIC_URL
    assert metadata["registration_endpoint"] == f"{PUBLIC_URL}/register"
    assert "S256" in metadata["code_challenge_methods_supported"]


async def test_an_unauthenticated_call_is_refused_and_points_at_the_metadata(
    http: httpx.AsyncClient,
) -> None:
    response = await http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 401
    assert "resource_metadata" in response.headers.get("www-authenticate", "")


async def test_the_owner_approves_a_client_and_it_can_then_call_tools(
    http: httpx.AsyncClient, grants: Grants
) -> None:
    verifier, challenge = _pkce()
    client_id = await _register(http)
    consent_url = await _authorize(http, client_id, challenge)
    assert consent_url.startswith("/consent?")

    page = await http.get(consent_url)
    assert page.status_code == 200
    assert "Claude" in page.text
    assert 'name="username"' in page.text and 'type="password"' in page.text
    assert "Nothing you type here is kept" in page.text
    assert "exclusive property of Toyota Motor Corporation" in page.text
    assert "not affiliated with, endorsed by" in page.text

    refused = await http.post(
        consent_url, data={"username": "driver@example.com", "password": "wrong"}
    )
    assert refused.status_code == 200
    assert "Toyota refused those credentials" in refused.text

    approved = await http.post(consent_url, data=CREDENTIALS)
    assert approved.status_code == 302
    redirect = httpx.URL(approved.headers["location"])
    assert str(redirect).startswith(REDIRECT_URI)
    assert redirect.params["state"] == "opaque-state"

    token_response = await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": redirect.params["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert token_response.status_code == 200, token_response.text
    token = token_response.json()
    assert token["token_type"] == "Bearer"
    assert token["scope"] == SCOPE

    call = await http.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
        headers={
            "Authorization": f"Bearer {token['access_token']}",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert call.status_code == 200, call.text
    assert "toyota" in call.text

    refreshed = await http.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": client_id,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["access_token"] != token["access_token"]


async def test_an_authorization_code_cannot_be_spent_twice(http: httpx.AsyncClient) -> None:
    verifier, challenge = _pkce()
    client_id = await _register(http)
    consent_url = await _authorize(http, client_id, challenge)
    approved = await http.post(consent_url, data=CREDENTIALS)
    code = httpx.URL(approved.headers["location"]).params["code"]
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    assert (await http.post("/token", data=body)).status_code == 200
    assert (await http.post("/token", data=body)).status_code == 400


async def test_a_wrong_pkce_verifier_is_refused(http: httpx.AsyncClient) -> None:
    _, challenge = _pkce()
    client_id = await _register(http)
    consent_url = await _authorize(http, client_id, challenge)
    approved = await http.post(consent_url, data=CREDENTIALS)
    response = await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": httpx.URL(approved.headers["location"]).params["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": "wrong-verifier",
        },
    )
    assert response.status_code == 400


async def test_an_expired_approval_link_says_so(http: httpx.AsyncClient) -> None:
    page = await http.get("/consent?request=unknown")
    assert page.status_code == 400
    assert "expired" in page.text


async def test_grants_survive_a_restart_and_are_written_privately(
    http: httpx.AsyncClient, grants: Grants
) -> None:
    verifier, challenge = _pkce()
    client_id = await _register(http)
    consent_url = await _authorize(http, client_id, challenge)
    approved = await http.post(consent_url, data=CREDENTIALS)
    await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": httpx.URL(approved.headers["location"]).params["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert oct(grants.path.stat().st_mode)[-3:] == "600"
    reloaded = Grants.load(grants.path)
    assert client_id in reloaded.clients
    assert reloaded.access_tokens and reloaded.refresh_tokens
    assert json.loads(grants.path.read_text())["clients"]


async def test_signing_in_with_another_toyota_account_is_refused(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, isolated_session: Path
) -> None:
    isolated_session.write_text(
        json.dumps({"username": "owner@example.com", "refresh_token": "r", "vin": None})
    )
    _, challenge = _pkce()
    client_id = await _register(http)
    consent_url = await _authorize(http, client_id, challenge)
    response = await http.post(consent_url, data=CREDENTIALS)
    assert response.status_code == 200
    assert "already connected to another Toyota account" in response.text


async def test_connecting_signs_in_to_toyota_then_lets_the_owner_pick_the_vehicle(
    http: httpx.AsyncClient,
    fake_controller_class: Any,
    isolated_session: Path,
) -> None:
    second = copy.deepcopy(fake_controller_class.responses["/v2/vehicle/guid"]["payload"][0])
    second["vin"] = "JTDZARBE0RJ000099"
    second["nickName"] = "Yaris"
    fake_controller_class.responses["/v2/vehicle/guid"]["payload"].append(second)

    verifier, challenge = _pkce()
    client_id = await _register(http)
    consent_url = await _authorize(http, client_id, challenge)

    signed_in = await http.post(consent_url, data=CREDENTIALS)
    assert signed_in.status_code == 200
    assert "Choose the vehicle" in signed_in.text
    assert "Corolla" in signed_in.text and "Yaris" in signed_in.text
    assert json.loads(isolated_session.read_text())["username"] == "driver@example.com"

    approved = await http.post(consent_url, data={"vin": "JTDZARBE0RJ000099"})
    assert approved.status_code == 302
    assert json.loads(isolated_session.read_text())["vin"] == "JTDZARBE0RJ000099"

    token = await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": httpx.URL(approved.headers["location"]).params["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text


async def test_a_single_vehicle_account_skips_the_picker(http: httpx.AsyncClient) -> None:
    _, challenge = _pkce()
    client_id = await _register(http)
    consent_url = await _authorize(http, client_id, challenge)
    approved = await http.post(consent_url, data=CREDENTIALS)
    assert approved.status_code == 302
    assert httpx.URL(approved.headers["location"]).params["code"]
