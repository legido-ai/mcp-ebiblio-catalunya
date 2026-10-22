"""MCP server for eBiblio Catalunya (Odilo).

Tools: find a publication, list its issues, borrow one issue, download it
(ACSM, deprotected to PDF/EPUB when libgourou is present), list loans and
return one.

The one tool an unattended job needs is ``fetch_issue``: it borrows the issue
for a date (today by default), downloads it, deprotects it, and reports where
the file landed. Everything it does is idempotent from the caller's side: an
issue already held is reused rather than borrowed twice.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import OdiloClient, OdiloError, RequestFailure, pretty_date
from .config import ConfigError, Settings
from .drm import AdeptToolchain, DrmError

log = logging.getLogger("odilo.server")

mcp = FastMCP("ebiblio-catalunya")

_client: OdiloClient | None = None
_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def get_client() -> OdiloClient:
    global _client
    if _client is None:
        _client = OdiloClient(get_settings())
    return _client


def _toolchain() -> AdeptToolchain:
    settings = get_settings()
    return AdeptToolchain(settings.adobe_dir, settings.libgourou_bin_dir)


@mcp.tool()
def find_publication(title: str, kind: str = "newspaper") -> dict[str, Any]:
    """Find a publication in the catalogue by title (accents and case ignored).

    Returns the record id, how many issues are available and the newest one.
    """
    record = get_client().find_serial(title, kind=kind)
    if record is None:
        return {"found": False, "title": title}
    return {"found": True, **record.to_dict()}


@mcp.tool()
def list_issues(title: str, limit: int = 14) -> dict[str, Any]:
    """List the issues a publication currently offers, newest first."""
    record = get_client().find_serial(title)
    if record is None:
        return {"found": False, "title": title, "issues": []}
    return {
        "found": True,
        "record_id": record.id,
        "title": record.title,
        "issues": [pretty_date(d) for d in record.issue_dates[:limit]],
        "newest": pretty_date(record.newest_issue) if record.newest_issue else None,
    }


@mcp.tool()
def borrow_issue(title: str, issue: str = "") -> dict[str, Any]:
    """Borrow one issue of a publication. `issue` is YYYYMMDD or YYYY-MM-DD.

    Empty `issue` means the newest issue on offer. An issue already held is
    returned as-is instead of being borrowed a second time.
    """
    client = get_client()
    record = client.find_serial(title)
    if record is None:
        return {"ok": False, "error": f"no catalogue record for {title!r}"}
    day = issue or record.newest_issue
    if not day:
        return {"ok": False, "error": f"{record.title!r} offers no issues"}
    held = client.checkout_for(record.id, day)
    if held is not None:
        return {"ok": True, "already_held": True, **held.to_dict()}
    checkout = client.borrow_issue(record.id, day)
    return {"ok": True, "already_held": False, **checkout.to_dict()}


@mcp.tool()
def fetch_issue(title: str, issue: str = "", deprotect: bool = True) -> dict[str, Any]:
    """Borrow an issue, download it, and deprotect it. The job-facing tool.

    Returns the path of the file. The ACSM expires within about five minutes,
    so it is fetched and fulfilled back to back in this one call; a loan taken
    this way cannot be returned afterwards, it just expires (60-120 minutes).
    Without libgourou the ACSM token is kept, but it will go stale.
    """
    client = get_client()
    settings = get_settings()
    record = client.find_serial(title)
    if record is None:
        return {"ok": False, "error": f"no catalogue record for {title!r}"}
    day = issue or record.newest_issue
    if not day:
        return {"ok": False, "error": f"{record.title!r} offers no issues"}
    checkout = client.checkout_for(record.id, day) or client.borrow_issue(record.id, day)
    acsm = client.download(checkout, dest_dir=settings.download_dir)
    result: dict[str, Any] = {
        "ok": True,
        "title": record.title,
        "record_id": record.id,
        "issue": pretty_date(day),
        "checkout_id": checkout.id,
        "acsm": str(acsm),
        "deprotected": None,
        "returned": False,
    }
    if deprotect:
        try:
            final = _toolchain().fulfil_acsm(acsm, Path(settings.download_dir) / "drm-free")
            result["deprotected"] = str(final)
            result["file"] = str(final)
        except DrmError as exc:
            result["deprotect_error"] = str(exc)
            result["file"] = str(acsm)
    else:
        result["file"] = str(acsm)
    return result


@mcp.tool()
def list_loans() -> dict[str, Any]:
    """List the loans currently held on the account."""
    loans = get_client().checkouts()
    return {"count": len(loans), "loans": [loan.to_dict() for loan in loans]}


@mcp.tool()
def return_loan(checkout_id: str) -> dict[str, Any]:
    """Return a loan by its checkout id.

    Only loans whose file was never downloaded are returnable; for the rest the
    platform answers HTTP 400 and the loan simply expires.
    """
    ok = get_client().return_checkout(checkout_id)
    return {"returned": ok, "checkout_id": checkout_id}


@mcp.tool()
def account() -> dict[str, Any]:
    """Who is logged in and what is currently held."""
    client = get_client()
    client.ensure_login()
    loans = client.checkouts()
    return {"patron": client.patron_name, "loans": len(loans)}


def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG if sys.argv[1:] and sys.argv[1] == "-v" else logging.INFO,
        stream=sys.stderr,
    )
    try:
        get_settings()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except (OdiloError, RequestFailure) as exc:  # pragma: no cover - startup guard
        print(f"startup failure: {exc}", file=sys.stderr)
        raise SystemExit(3) from exc
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
