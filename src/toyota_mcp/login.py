from __future__ import annotations

import asyncio

from pytoyoda.exceptions import ToyotaLoginError

from toyota_mcp.session import (
    Session,
    SessionStore,
    authorization_code,
    authorize_url,
    exchange,
    open_browser,
)

EXIT_OK = 0
EXIT_CANCELLED = 1
EXIT_AUTH = 3

PROMPT = (
    "\nSign in on the page that just opened. Toyota then redirects to an address your\n"
    "browser cannot follow (it starts with com.toyota.oneapp:) and shows an error — that\n"
    "is expected. Copy that address from the address bar and paste it here.\n\n"
    "Redirect address (or just the code): "
)


def run(store: SessionStore | None = None, prompt: object = input) -> int:
    store = store or SessionStore()
    url = authorize_url()
    if not open_browser(url):
        print("Could not open a browser. Open this address yourself:")
        print(f"  {url}")
    try:
        pasted = prompt(PROMPT)  # type: ignore[operator]
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return EXIT_CANCELLED
    try:
        code = authorization_code(str(pasted))
    except ValueError as exc:
        print(f"Nothing usable in what you pasted: {exc}")
        return EXIT_CANCELLED
    try:
        session = asyncio.run(exchange(code))
    except ToyotaLoginError as exc:
        print(str(exc))
        return EXIT_AUTH
    store.save(session)
    who = f" for {session.username}" if session.username else ""
    print(f"Signed in{who}. The session is saved in your system credential store;")
    print("your password was never seen by this program. Check it with `toyota-mcp doctor`.")
    return EXIT_OK


def logout(store: SessionStore | None = None) -> int:
    store = store or SessionStore()
    if (store or SessionStore()).clear():
        print("Saved session removed.")
    else:
        print("There was no saved session.")
    return EXIT_OK


__all__ = ["EXIT_AUTH", "EXIT_CANCELLED", "EXIT_OK", "Session", "logout", "run"]
