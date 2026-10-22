"""The DRM helpers that can be tested without libgourou installed."""

from __future__ import annotations

from pathlib import Path

import pytest

from odilo_ebiblio.drm import AdeptToolchain, DrmError, _sniff, _binary


def test_sniff_detects_pdf_and_epub(tmp_path: Path):
    pdf = tmp_path / "a"
    pdf.write_bytes(b"%PDF-1.7\n...")
    epub = tmp_path / "b"
    epub.write_bytes(b"PK\x03\x04zip")
    other = tmp_path / "c"
    other.write_bytes(b"\x00\x01")
    assert _sniff(pdf) == ".pdf"
    assert _sniff(epub) == ".epub"
    assert _sniff(other) == ".bin"


def test_missing_binary_names_the_variable():
    with pytest.raises(DrmError) as err:
        _binary("definitely-not-installed-xyz", None)
    assert "EBIBLIO_LIBGOUROU_BIN_DIR" in str(err.value)


def test_activation_state_is_read_from_the_device_files(tmp_path: Path):
    toolchain = AdeptToolchain(tmp_path / ".adobe")
    assert toolchain.activated is False
    (tmp_path / ".adobe").mkdir(parents=True)
    (tmp_path / ".adobe" / "device.xml").write_text("<device/>")
    assert toolchain.activated is False
    (tmp_path / ".adobe" / "activation.xml").write_text("<activation/>")
    assert toolchain.activated is True
