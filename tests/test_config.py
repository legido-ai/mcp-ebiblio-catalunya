"""Configuration comes from the environment and never from the tree."""

from __future__ import annotations

import pytest

from odilo_ebiblio.config import DEFAULT_BASE_URL, ConfigError, Settings


def test_credentials_from_the_catalan_names():
    settings = Settings.from_env({"CATALUNYA_EBIBLIO_USERNAME": "c", "CATALUNYA_EBIBLIO_PASSWORD": "p"})
    assert settings.username == "c" and settings.password == "p"
    assert settings.base_url == DEFAULT_BASE_URL
    assert settings.api_url == DEFAULT_BASE_URL + "/opac/api/v2"


def test_generic_names_win_first():
    settings = Settings.from_env({
        "EBIBLIO_USERNAME": "generic", "CATALUNYA_EBIBLIO_USERNAME": "catalan",
        "EBIBLIO_PASSWORD": "g", "CATALUNYA_EBIBLIO_PASSWORD": "p",
    })
    assert settings.username == "generic"


def test_missing_credentials_raise():
    with pytest.raises(ConfigError) as err:
        Settings.from_env({})
    assert "credentials missing" in str(err.value)


def test_base_url_and_dirs_are_overridable(tmp_path):
    settings = Settings.from_env({
        "CATALUNYA_EBIBLIO_USERNAME": "c", "CATALUNYA_EBIBLIO_PASSWORD": "p",
        "EBIBLIO_BASE_URL": "https://example.test/", "EBIBLIO_DATA_DIR": str(tmp_path),
    })
    assert settings.api_url == "https://example.test/opac/api/v2"
    assert settings.download_dir == tmp_path / "downloads"
    assert settings.adobe_dir == tmp_path / ".adobe"
