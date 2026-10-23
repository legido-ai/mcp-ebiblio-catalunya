"""Runtime configuration for the eBiblio Catalunya (Odilo) MCP server.

Secrets come from the environment only, never from files committed to the
repository.  See ``.env.example`` for the full list of variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: eBiblio Catalunya runs on Odilo. This is the host that actually answers;
#: ``catalunya.ebiblio.cat`` has no DNS record for API clients.
DEFAULT_BASE_URL = "https://biblioteca.ebiblio.cat"

#: Odilo's own media player. An album loan hands out a ``mediaId`` + per-checkout
#: ``keyId`` that this player exchanges for the album's track list.
DEFAULT_PLAYER_URL = "https://player.eu.odilo.io"

#: Odilo's public client credentials, taken from the anonymous JavaScript
#: bundle the SPA ships to every visitor. Not a secret: it only mints the
#: read-only client token used for catalogue browsing.
DEFAULT_CLIENT_BASIC = "NgOpac:kvFqFy1i0Y4eUJhM"

DEFAULT_DATA_DIR = "/data"

#: The SPA's own Accept value. Odilo's login endpoint answers HTTP 500 for
#: every credential when this exact header is missing, which is easy to
#: misread as a broken account.
SPA_ACCEPT = "application/json, text/plain, */*"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded;charset=UTF-8"

#: Aliases so the same image can serve any Catalan deployment; first set wins.
_USERNAME_ALIASES = ("EBIBLIO_USERNAME", "CATALUNYA_EBIBLIO_USERNAME")
_PASSWORD_ALIASES = ("EBIBLIO_PASSWORD", "CATALUNYA_EBIBLIO_PASSWORD")


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or malformed."""


def _first_env(env: dict[str, str], names: tuple[str, ...]) -> tuple[str | None, str | None]:
    for name in names:
        value = env.get(name)
        if value:
            return name, value
    return None, None


@dataclass(frozen=True)
class Settings:
    """Validated runtime settings."""

    username: str
    password: str
    base_url: str = DEFAULT_BASE_URL
    client_basic: str = DEFAULT_CLIENT_BASIC
    data_dir: Path = Path(DEFAULT_DATA_DIR)
    adept_dir: Path | None = None
    libgourou_bin_dir: Path | None = None
    player_url: str = DEFAULT_PLAYER_URL
    timeout: int = 120

    @property
    def api_url(self) -> str:
        return self.base_url.rstrip("/") + "/opac/api/v2"

    @property
    def download_dir(self) -> Path:
        return Path(self.data_dir) / "downloads"

    @property
    def music_dir(self) -> Path:
        """Where borrowed albums land: one directory per catalogue record."""
        return Path(self.data_dir) / "music"

    @property
    def adobe_dir(self) -> Path:
        return Path(self.adept_dir) if self.adept_dir else Path(self.data_dir) / ".adobe"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        user_key, username = _first_env(env, _USERNAME_ALIASES)
        pass_key, password = _first_env(env, _PASSWORD_ALIASES)
        if not username or not password:
            raise ConfigError(
                "credentials missing: set "
                + " / ".join(_USERNAME_ALIASES)
                + " and "
                + " / ".join(_PASSWORD_ALIASES)
            )
        return cls(
            username=username,
            password=password,
            base_url=env.get("EBIBLIO_BASE_URL") or DEFAULT_BASE_URL,
            client_basic=env.get("EBIBLIO_CLIENT_BASIC") or DEFAULT_CLIENT_BASIC,
            data_dir=Path(env.get("EBIBLIO_DATA_DIR") or DEFAULT_DATA_DIR),
            adept_dir=Path(env["EBIBLIO_ADEPT_DIR"]) if env.get("EBIBLIO_ADEPT_DIR") else None,
            libgourou_bin_dir=(
                Path(env["EBIBLIO_LIBGOUROU_BIN_DIR"])
                if env.get("EBIBLIO_LIBGOUROU_BIN_DIR")
                else None
            ),
            timeout=int(env.get("EBIBLIO_TIMEOUT") or 120),
            player_url=env.get("EBIBLIO_PLAYER_URL") or DEFAULT_PLAYER_URL,
        )

    def credential_sources(self) -> tuple[str, str]:
        """Names of the variables the credentials came from (for diagnostics)."""
        return (_USERNAME_ALIASES[0], _PASSWORD_ALIASES[0])
