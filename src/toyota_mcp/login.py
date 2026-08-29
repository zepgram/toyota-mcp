from __future__ import annotations

import asyncio
from collections.abc import Callable
from getpass import getpass

from pytoyoda.exceptions import ToyotaLoginError

from toyota_mcp.session import Session, SessionStore, sign_in

EXIT_OK = 0
EXIT_CANCELLED = 1
EXIT_AUTH = 3

PREAMBLE = (
    "Sign in with the MyToyota account the car is paired to. The password goes straight to "
    "Toyota and is not written anywhere; only the session token it returns is kept.\n"
)


def run(
    store: SessionStore | None = None,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass,
) -> int:
    store = store or SessionStore()
    print(PREAMBLE)
    try:
        username = ask("MyToyota email address: ")
        password = ask_secret("Password: ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return EXIT_CANCELLED
    try:
        session = asyncio.run(sign_in(username, password))
    except ToyotaLoginError as exc:
        print(f"Toyota refused the sign-in: {exc}")
        return EXIT_AUTH
    store.save(session)
    print(f"Signed in as {session.username}. The session is kept in {store.location}.")
    print("Check it with `toyota-mcp doctor`.")
    return EXIT_OK


def logout(store: SessionStore | None = None) -> int:
    store = store or SessionStore()
    print("Saved session removed." if store.clear() else "There was no saved session.")
    return EXIT_OK


__all__ = ["EXIT_AUTH", "EXIT_CANCELLED", "EXIT_OK", "Session", "logout", "run"]
