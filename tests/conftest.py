"""Shared test fixtures: a recording, scripted HTTP transport.

The client takes its transport by injection, so every test here runs with no
network and no credentials — the shapes being asserted are the ones read out of
the platform's own JavaScript bundle.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from odilo_ebiblio.client import OdiloClient  # noqa: E402
from odilo_ebiblio.config import Settings  # noqa: E402


class FakeResponse:
    def __init__(self, status_code: int = 200, *, json_body: Any = None,
                 text: str | None = None, content: bytes | None = None,
                 headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._json = json_body
        self.text = text if text is not None else json.dumps(json_body or {})
        self.content = content if content is not None else self.text.encode()
        self.headers = headers or {}

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no json in this response")
        return self._json


class FakeSession:
    """Records every call and answers from a script keyed by URL fragment."""

    def __init__(self, script: list[tuple[str, FakeResponse]] | None = None,
                 default: FakeResponse | None = None) -> None:
        self.script = list(script or [])
        self.default = default or FakeResponse(200, json_body={})
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        # Script entries stay in place: the first matching fragment answers every
        # call that matches it, so repeated reads of the same route work.
        for fragment, response in self.script:
            if fragment in url:
                return response
        return self.default

    def called(self, fragment: str) -> dict[str, Any] | None:
        for call in self.calls:
            if fragment in call["url"]:
                return call
        return None


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        username="CARD", password="PIN", data_dir=tmp_path,
        adept_dir=tmp_path / ".adobe",
    )


@pytest.fixture()
def token_response() -> FakeResponse:
    return FakeResponse(200, json_body={"token": "abcdef0123456789"})


@pytest.fixture()
def logged_in(settings: Settings, token_response: FakeResponse) -> FakeSession:
    """A session already past /token and /login."""
    session = FakeSession([
        ("/token", token_response),
        ("/login", FakeResponse(200, json_body={"id": "360054490", "name": "Card Holder"})),
    ])
    client = OdiloClient(settings, transport=session)
    client.login()
    return session
