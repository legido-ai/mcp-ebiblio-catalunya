"""Adobe ADEPT (ACS4) fulfilment with libgourou.

Odilo hands out the same ACS4 flavour of DRM as the rest of eBiblio: the loan
file is an ``.acsm`` fulfilment token that only Adobe software can open. This
module turns it into a plain, DRM-free file with `libgourou`, an independent
implementation of the ADEPT protocol:

1. ``adept_activate --anonymous`` creates a device key (no Adobe account, the
   same "authorise without an ID" mode Adobe Digital Editions offers);
2. ``acsmdownloader`` fulfils the token into the encrypted publication;
3. ``adept_remove`` strips the DRM.

Newspaper issues come back as PDF, books as EPUB, so the suffix is decided by
the payload's own magic rather than by assumption.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

log = logging.getLogger("odilo.drm")

ACTIVATE = "adept_activate"
DOWNLOAD = "acsmdownloader"
REMOVE = "adept_remove"
LOAN_MGT = "adept_loan_mgt"


class DrmError(RuntimeError):
    """Raised when the ADEPT toolchain cannot complete an operation."""


def _binary(name: str, bin_dir: Path | None) -> str:
    if bin_dir:
        candidate = Path(bin_dir) / name
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if not found:
        raise DrmError(f"{name} not found: install libgourou or set EBIBLIO_LIBGOUROU_BIN_DIR")
    return found


def _tail(text: str, limit: int = 400) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[-limit:] if text else "no output"


def parse_loan_list(text: str) -> list[dict[str, str]]:
    """Parse ``adept_loan_mgt -l`` output into loan entries.

    The tool prints a title row, a dashed rule and then one row per loan::

               ID            Book          Expiration
        --------------------------------------------------
        f59c20db0eabf044    El Pas         (Expired)
    """
    loans: list[dict[str, str]] = []
    for line in (text or "").splitlines():
        row = line.strip()
        if not row or set(row) <= {"-", " "} or row.upper().startswith("ID"):
            continue
        parts = row.split()
        # A real row is "<hex id> <title...> (<expiration>)"; this also skips the
        # "No books loaned" line the tool prints when there is nothing to show.
        if len(parts) < 3 or not re.fullmatch(r"[0-9a-f]{8,}", parts[0]):
            continue
        if not parts[-1].startswith("("):
            continue
        loans.append({
            "loan_id": parts[0],
            "title": " ".join(parts[1:-1]),
            "expiration": parts[-1],
        })
    return loans


def _sniff(path: Path) -> str:
    """Suffix for a fulfilled file, from its own magic bytes."""
    try:
        head = Path(path).read_bytes()[:8]
    except OSError:
        return ".bin"
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"PK\x03\x04"):
        return ".epub"
    return ".bin"


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    """Serialise access to the shared ADEPT device directory."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class AdeptToolchain:
    """Thin wrapper around the libgourou command line utilities."""

    def __init__(self, adept_dir: Path, bin_dir: Path | None = None, timeout: int = 300) -> None:
        self.adept_dir = Path(adept_dir)
        self.bin_dir = Path(bin_dir) if bin_dir else None
        self.timeout = timeout

    @property
    def activated(self) -> bool:
        return (self.adept_dir / "device.xml").is_file() and (
            self.adept_dir / "activation.xml"
        ).is_file()

    def ensure_activated(self) -> None:
        if self.activated:
            return
        with _locked(self.adept_dir.parent / "adept.lock"):
            if self.activated:
                return
            self.adept_dir.parent.mkdir(parents=True, exist_ok=True)
            if self.adept_dir.exists():
                shutil.rmtree(self.adept_dir)
            cmd = [
                _binary(ACTIVATE, self.bin_dir),
                "--anonymous",
                "--random-serial",
                "--output-dir",
                str(self.adept_dir),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
            if result.returncode != 0 or not self.activated:
                raise DrmError("device activation failed: " + _tail(result.stderr or result.stdout))
            os.chmod(self.adept_dir, 0o700)
            log.info("created an anonymous ADEPT device in %s", self.adept_dir)

    def fulfil_acsm(self, acsm_path: Path, work_dir: Path) -> Path:
        """Turn an .acsm loan token into a DRM-free file."""
        self.ensure_activated()
        acsm_path = Path(acsm_path)
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        encrypted = work_dir / (acsm_path.stem + ".enc")
        cmd = [
            _binary(DOWNLOAD, self.bin_dir),
            "--adept-directory", str(self.adept_dir),
            "--output-file", str(encrypted),
            str(acsm_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        if result.returncode != 0 or not encrypted.is_file():
            raise DrmError(
                "downloading the loaned publication failed: " + _tail(result.stderr or result.stdout)
            )
        removed = work_dir / (acsm_path.stem + ".drm-free" + _sniff(encrypted))
        cmd = [
            _binary(REMOVE, self.bin_dir),
            "--adept-directory", str(self.adept_dir),
            "--output-file", str(removed),
            str(encrypted),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        if result.returncode != 0 or not removed.is_file():
            raise DrmError("DRM removal failed: " + _tail(result.stderr or result.stdout))
        encrypted.unlink(missing_ok=True)
        log.info("DRM removed into %s", removed)
        return removed

    # -------------------------------------------------------------- loan handling
    def loans(self) -> list[dict[str, str]]:
        """Loans libgourou has recorded, from ``adept_loan_mgt --list``."""
        result = subprocess.run(
            [_binary(LOAN_MGT, self.bin_dir), "--adept-directory", str(self.adept_dir), "--list"],
            capture_output=True, text=True, timeout=self.timeout,
        )
        if result.returncode != 0:
            raise DrmError("listing loans failed: " + _tail(result.stderr or result.stdout))
        return parse_loan_list(result.stdout or result.stderr)

    def return_loan(self, loan_id: str) -> bool:
        """Return a loan to the vendor through the Adobe protocol.

        This is the return that works for an issue whose ACSM was fulfilled:
        the vendor's fulfilment server is notified and the loan disappears from
        the library account, even though the library's own web API refuses to
        return an ACSM loan directly. ``loan_id`` comes from :meth:`loans`.

        Two invocation details are not optional, both measured against the live
        tool: the **long** form (``--return <id>``) prints nothing, exits 0 and
        returns nothing at all — the loan stays in the list — so the short
        ``-r <id>`` is the one that works; and ``-r`` asks for confirmation, so
        ``y`` has to be written to stdin or it reads EOF and does nothing.
        """
        self.ensure_activated()
        result = subprocess.run(
            [_binary(LOAN_MGT, self.bin_dir), "-D", str(self.adept_dir), "-r", loan_id],
            capture_output=True, text=True, input="y\n", timeout=self.timeout,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 or "successfully returned" not in output.lower():
            raise DrmError("returning the loan failed: " + _tail(output))
        log.info("loan %s returned to the vendor", loan_id)
        return True

    def return_latest(self) -> str | None:
        """Return the most recently fulfilled loan; returns its id, or None."""
        loans = self.loans()
        if not loans:
            return None
        loan_id = loans[-1]["loan_id"]
        self.return_loan(loan_id)
        return loan_id


def convert_ebook(source: Path, target_format: str, out_path: Path,
                  ebook_convert: str | None = None) -> Path:
    """Convert with Calibre's ``ebook-convert`` (EPUB -> MOBI/AZW3)."""
    target_format = target_format.lower().lstrip(".")
    binary = ebook_convert or shutil.which("ebook-convert")
    if not binary:
        raise DrmError("ebook-convert (Calibre) is not installed: only epub export is available")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([binary, str(source), str(out_path)],
                            capture_output=True, text=True, timeout=900)
    if result.returncode != 0 or not out_path.is_file():
        raise DrmError(f"converting to {target_format} failed: " + _tail(result.stderr or result.stdout))
    return out_path
