"""The DRM helpers that can be tested without libgourou installed."""

from __future__ import annotations

from pathlib import Path

import pytest

from odilo_ebiblio.drm import (
    AdeptToolchain,
    DrmError,
    _sniff,
    _binary,
    parse_loan_list,
)

LOAN_LIST_OUTPUT = """
       ID            Book          Expiration
--------------------------------------------------
f59c20db0eabf044    El Pas         (Expired)
"""


def _activated(tmp_path: Path) -> AdeptToolchain:
    adept = tmp_path / ".adobe"
    adept.mkdir(parents=True, exist_ok=True)
    (adept / "device.xml").write_text("<device/>")
    (adept / "activation.xml").write_text("<activation/>")
    return AdeptToolchain(adept)


def test_parse_loan_list_reads_id_title_and_expiration():
    loans = parse_loan_list(LOAN_LIST_OUTPUT)
    assert loans == [{"loan_id": "f59c20db0eabf044", "title": "El Pas", "expiration": "(Expired)"}]
    assert parse_loan_list("No books loaned\n") == []


def test_parse_loan_list_accepts_a_title_truncated_inside_parentheses():
    """The tool cuts the title at a fixed width, so the wrap can be lost.

    Measured against the live tool: a long title ending in a parenthesised
    subtitle comes back as "... (Los mejores cl" followed by a bare timestamp.
    The old parser required the last field to start with "(", so it dropped the
    row entirely and the loan was never returned.
    """
    output = """       ID                        Book                      Expiration
--------------------------------------------------------------------------
f56ed52662b36028    Cañas y barro (Los mejores cl    2026-11-22T11:28:48Z
"""
    loans = parse_loan_list(output)
    assert [loan["loan_id"] for loan in loans] == ["f56ed52662b36028"]
    assert loans[0]["expiration"] == "2026-11-22T11:28:48Z"


def test_return_loan_calls_the_loan_manager_with_the_id(tmp_path, monkeypatch):
    captured: dict = {}

    class Result:
        returncode = 0
        stdout = "Loan f59c20db0eabf044 successfully returned\n"
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return Result()

    monkeypatch.setattr("odilo_ebiblio.drm.subprocess.run", fake_run)
    monkeypatch.setattr("odilo_ebiblio.drm._binary", lambda name, bin_dir: name)
    assert _activated(tmp_path).return_loan("f59c20db0eabf044") is True
    assert captured["cmd"][0] == "adept_loan_mgt"
    # The long form (--return) exits 0 and returns nothing at all: only -r works,
    # and it asks for confirmation, so the answer has to be written to stdin.
    assert "-r" in captured["cmd"] and "f59c20db0eabf044" in captured["cmd"]
    assert "--return" not in captured["cmd"]
    assert captured["input"] == "y\n"


def test_return_loan_reports_a_refusal(tmp_path, monkeypatch):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "Error : something went wrong"

    monkeypatch.setattr("odilo_ebiblio.drm.subprocess.run", lambda cmd, **kw: Result())
    monkeypatch.setattr("odilo_ebiblio.drm._binary", lambda name, bin_dir: name)
    with pytest.raises(DrmError) as err:
        _activated(tmp_path).return_loan("abc")
    assert "returning the loan failed" in str(err.value)


def test_return_latest_returns_the_last_recorded_loan(tmp_path, monkeypatch):
    class ListResult:
        returncode = 0
        stdout = LOAN_LIST_OUTPUT
        stderr = ""

    returned: list[str] = []

    toolchain = _activated(tmp_path)
    monkeypatch.setattr("odilo_ebiblio.drm._binary", lambda name, bin_dir: name)
    monkeypatch.setattr("odilo_ebiblio.drm.AdeptToolchain.loans", lambda self: parse_loan_list(LOAN_LIST_OUTPUT))
    monkeypatch.setattr("odilo_ebiblio.drm.AdeptToolchain.return_loan",
                        lambda self, loan_id: returned.append(loan_id) or True)
    assert toolchain.return_latest() == "f59c20db0eabf044"
    assert returned == ["f59c20db0eabf044"]
    monkeypatch.setattr("odilo_ebiblio.drm.AdeptToolchain.loans", lambda self: [])
    assert toolchain.return_latest() is None


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
