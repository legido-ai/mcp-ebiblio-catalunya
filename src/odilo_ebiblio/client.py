"""eBiblio Catalunya (Odilo) client.

Every call shape here was read out of the platform's own JavaScript bundle and
then verified against the live service:

    POST /token                          grant_type=client_credentials -> bearer
    POST /login                          form userId/password -> cookies + patron id
    GET  /records?query=...              -> {"records": [...], "total": N}
    POST /records/{rid}/checkout         ?patronId=&from=RECORD_SCREEN&issueDate=YYYYMMDD
                                         (the same query string is also the form body)
    GET  {checkout.download_url}         -> application/vnd.adobe.adept+xml (ACSM)
    GET  /patrons/{pid}/checkouts        -> open loans
    POST /checkouts/{cid}/return         body/query patronId=

Two traps worth remembering:

* readings live under ``records``, not ``data`` — reading ``.data`` quietly
  yields zero results;
* the Accept header must be the SPA's own value, or ``/login`` answers HTTP 500
  for every credential, real or dummy.
"""

from __future__ import annotations

import base64
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol
from urllib.parse import urljoin

import requests

from .config import SPA_ACCEPT, USER_AGENT, Settings

log = logging.getLogger("odilo.client")

#: Screen enum the platform expects in ``from=``; the bundle uses the router's
#: component to pick one. The record page is what a catalogue-driven borrow is.
DEFAULT_SCREEN = "RECORD_SCREEN"

#: Spaces, case and accents must not stop "El País" matching "El Pais".
_PUNCT = re.compile(r"[^0-9a-z ]+")
_WS = re.compile(r"\s+")


class OdiloError(RuntimeError):
    """Raised for any expected failure while talking to Odilo."""


class AuthenticationError(OdiloError):
    """Login was refused, or the session expired and could not be renewed."""


def normalise_title(text: str) -> str:
    """Casefold, strip accents and punctuation so titles compare as words."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = _PUNCT.sub(" ", ascii_only.casefold())
    return _WS.sub(" ", cleaned).strip()


def issue_date(value: str | date | datetime) -> str:
    """Normalise a date to Odilo's YYYYMMDD form."""
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    digits = re.sub(r"\D", "", str(value))
    if len(digits) != 8:
        raise ValueError(f"not a YYYYMMDD issue date: {value!r}")
    return digits


def pretty_date(value: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD for human output."""
    value = str(value)
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}" if len(value) == 8 else value


@dataclass
class Record:
    """One catalogue record (a newspaper, a magazine, a book...)."""

    id: str
    title: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def resource_types(self) -> list[str]:
        return list(self.raw.get("resourceTypes") or [])

    @property
    def formats(self) -> list[str]:
        return list(self.raw.get("formats") or [])

    @property
    def issue_dates(self) -> list[str]:
        """Available issues, newest first."""
        dates = [str(d) for d in (self.raw.get("availableIssueDates") or [])]
        return sorted(dates, reverse=True)

    @property
    def newest_issue(self) -> str | None:
        dates = self.issue_dates
        return dates[0] if dates else None

    @property
    def kind(self) -> str:
        """newspaper / magazine / ebook / ... derived from resourceTypes."""
        for candidate in ("newspaper", "magazine"):
            if any(candidate.upper() in t.upper() for t in self.resource_types):
                return candidate
        return "other"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "formats": self.formats,
            "newest_issue": self.newest_issue,
            "issue_dates": self.issue_dates[:30],
        }


@dataclass
class Checkout:
    """One loan held by the patron."""

    id: str
    record_id: str
    issue_date: str | None = None
    download_url: str | None = None
    formats: list[str] = field(default_factory=list)
    returnable: bool = False
    expired: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Checkout":
        return cls(
            id=str(data.get("id")),
            record_id=str(data.get("recordId") or data.get("resourceId") or ""),
            issue_date=str(data["issueDate"]) if data.get("issueDate") else None,
            download_url=data.get("downloadUrl"),
            formats=list(data.get("formats") or []),
            returnable=bool(data.get("returnable")),
            expired=bool(data.get("expired")),
            raw=data,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkout_id": self.id,
            "record_id": self.record_id,
            "issue_date": self.issue_date,
            "returnable": self.returnable,
            "expired": self.expired,
        }


class Transport(Protocol):
    """Minimal HTTP surface, so tests can drive the client without a network."""

    def request(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        ...


class RequestFailure(OdiloError):
    """A non-2xx response, carrying the status and a short excerpt."""

    def __init__(self, status: int, url: str, body: str) -> None:
        self.status = status
        self.url = url
        self.body = body
        detail = re.sub(r"\s+", " ", (body or "").strip())[:200]
        super().__init__(f"HTTP {status} for {url}: {detail}")


class OdiloClient:
    """Patron and catalogue operations against eBiblio Catalunya."""

    def __init__(self, settings: Settings, transport: Transport | None = None) -> None:
        self.settings = settings
        self.session: Transport = transport or requests.Session()
        self._token: str | None = None
        self._token_at: float = 0.0
        self.patron_id: str | None = None
        self.patron_name: str | None = None

    # ------------------------------------------------------------------ plumbing
    def _headers(self, *, auth: bool = True, form: bool = False) -> dict[str, str]:
        headers = {
            "Accept": SPA_ACCEPT,
            "User-Agent": USER_AGENT,
            "Origin": self.settings.base_url,
            "Referer": self.settings.base_url + "/",
        }
        if form:
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
        if auth:
            headers["Authorization"] = f"Bearer {self.token()}"
        return headers

    def _request(self, method: str, path: str, *, form: str | None = None,
                 auth: bool = True, allow: tuple[int, ...] = (200,)) -> Any:
        url = path if path.startswith("http") else self.settings.api_url + path
        kwargs: dict[str, Any] = {"headers": self._headers(auth=auth, form=form is not None)}
        if form is not None:
            kwargs["data"] = form
        response = self.session.request(method, url, timeout=self.settings.timeout, **kwargs)
        if response.status_code not in allow:
            raise RequestFailure(response.status_code, url, response.text)
        return response

    def token(self) -> str:
        """Client token, cached for the process lifetime.

        The token endpoint authenticates with HTTP Basic rather than the bearer
        token, because there is nothing to bear yet.
        """
        if self._token and time.time() - self._token_at < 3000:
            return self._token
        basic = base64.b64encode(self.settings.client_basic.encode()).decode()
        response = self.session.request(
            "POST",
            self.settings.api_url + "/token",
            data="grant_type=client_credentials",
            timeout=self.settings.timeout,
            headers={
                "Accept": SPA_ACCEPT,
                "User-Agent": USER_AGENT,
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if response.status_code != 200:
            raise RequestFailure(response.status_code, "POST /token", response.text)
        payload = response.json()
        self._token = payload.get("token")
        self._token_at = time.time()
        if not self._token:
            raise AuthenticationError("client token missing from /token response")
        return self._token

    # -------------------------------------------------------------------- patron
    def login(self) -> dict[str, Any]:
        """Authenticate the patron; the session cookie is what patron routes need."""
        token = self.token()
        response = self._request(
            "POST", "/login",
            form=f"userId={self.settings.username}&password={self.settings.password}",
            allow=(200, 401),
        )
        if response.status_code == 401:
            raise AuthenticationError(f"login refused: {response.text[:160]}")
        payload = response.json()
        self.patron_id = str(payload.get("id") or "") or None
        self.patron_name = payload.get("name")
        if not self.patron_id:
            raise AuthenticationError("login response carried no patron id")
        log.debug("logged in as patron %s", self.patron_id)
        return payload

    def ensure_login(self) -> str:
        if not self.patron_id:
            self.login()
        return self.patron_id or ""

    # ---------------------------------------------------------------- catalogue
    def search(self, query: str, *, limit: int = 24, offset: int = 0) -> list[Record]:
        params = {
            "limit": limit, "offset": offset, "query": query,
            "order": "fechaModifica_dt:desc", "faceted": "true", "availability": "true",
            "lists": "true", "showExperiences": "true", "enableBiblioFacet": "true",
            "limitFacetValues": 16,
        }
        from urllib.parse import urlencode

        response = self._request("GET", "/records?" + urlencode(params))
        payload = response.json()
        rows = payload.get("records") or payload.get("data") or []
        return [Record(id=str(r.get("id")), title=str(r.get("title") or ""), raw=r) for r in rows]

    def record(self, record_id: str) -> Record:
        response = self._request("GET", f"/records/{record_id}")
        payload = response.json()
        return Record(id=str(payload.get("id") or record_id),
                      title=str(payload.get("title") or ""), raw=payload)

    def find_serial(self, title: str, *, kind: str = "newspaper") -> Record | None:
        """Find the catalogue record for a publication, ignoring accents and case.

        Matching is done on normalised titles, so a caller can ask for "El País"
        and get the same publication whatever the platform calls it locally.
        """
        wanted = normalise_title(title)
        best: Record | None = None
        for candidate in self.search(title, limit=24):
            if normalise_title(candidate.title) != wanted:
                continue
            if kind and candidate.kind != kind:
                continue
            if candidate.issue_dates:
                return candidate
            best = best or candidate
        return best

    # -------------------------------------------------------------------- loans
    def borrow_issue(self, record_id: str, issue: str, *, screen: str = DEFAULT_SCREEN) -> Checkout:
        """Borrow a dated issue of a newspaper or magazine."""
        patron = self.ensure_login()
        day = issue_date(issue)
        query = f"patronId={patron}&from={screen}&issueDate={day}"
        response = self._request("POST", f"/records/{record_id}/checkout?{query}", form=query)
        return Checkout.from_json(response.json())

    def checkouts(self) -> list[Checkout]:
        patron = self.ensure_login()
        response = self._request("GET", f"/patrons/{patron}/checkouts")
        payload = response.json()
        rows = payload if isinstance(payload, list) else payload.get("records") or payload.get("data") or []
        return [Checkout.from_json(r) for r in rows]

    def checkout_for(self, record_id: str, issue: str | None = None) -> Checkout | None:
        day = issue_date(issue) if issue else None
        for loan in self.checkouts():
            if str(loan.record_id) != str(record_id):
                continue
            if day and str(loan.issue_date or "") != day:
                continue
            return loan
        return None

    def return_checkout(self, checkout_id: str) -> bool:
        """Return a loan. Only possible while it is still returnable.

        Loans are returnable — the return button exists and it works. What
        removes the option is the ACSM download: the platform's own code sets
        ``returnable = false`` when the download format includes ``ACSM``
        (verified in the bundle: ``downloadFormat.includes("ACSM") && (…returnable
        = false)``), and the server then answers HTTP 400 on every attempt.

        So an issue delivered as OCS can be fetched *and* returned, while one
        fulfilled from its ACSM cannot — it simply expires, which for a
        newspaper loan takes 60–120 minutes.
        """
        patron = self.ensure_login()
        query = f"patronId={patron}"
        response = self._request(
            "POST", f"/checkouts/{checkout_id}/return?{query}", form=query, allow=(200, 400),
        )
        return response.status_code == 200

    def checkout_token(self, checkout: Checkout) -> str | None:
        """The per-checkout token Odilo embeds in ``downloadUrl``.

        The download endpoint rejects anything else: the client token earns
        ``400 ["Invalid checkout token ..."]``.
        """
        if not checkout.download_url:
            return None
        from urllib.parse import parse_qs, urlparse

        return parse_qs(urlparse(checkout.download_url).query).get("token", [None])[0]

    def download(self, checkout: Checkout, *, format: str = "ACSM", dest_dir: Any = None) -> Any:
        """Fetch the loan file and save it; returns the saved path.

        The three formats Odilo offers for a newspaper loan are very different:

        ``ACSM`` (default)
            the Adobe fulfilment token (~1.3 KB). It **expires within about five
            minutes**, so it must be fulfilled immediately (``drm.fulfil_acsm``)
            — a token left lying around answers ``E_ADEPT_REQUEST_EXPIRED``.
            Fulfilling it yields the complete issue (~22 MB, 56 pages for El
            País). Fetching the ACSM also makes the loan non-returnable.
        ``OCS``
            a large PDF (~20 MB) that is, despite the name, **AES-256
            encrypted** with an Odilo policy key; only their reader opens it.
            The loan stays returnable.
        ``PDF``
            an HTML reader wrapper (``nubereader-pdfs.odilotk.es``), not a file.
        """
        import pathlib

        wanted = (format or "ACSM").upper()
        if wanted == "ACSM" and checkout.download_url:
            response = self._request("GET", checkout.download_url, auth=False, allow=(200,))
            blob = response.content
        else:
            token = self.checkout_token(checkout)
            if not token:
                raise OdiloError("checkout carries no downloadUrl; re-borrow or re-list loans")
            from urllib.parse import urlencode

            query = urlencode({
                "patronId": self.ensure_login(), "format": wanted, "token": token,
                "redirect": "no", "extractUrl": "yes", "recordId": checkout.record_id,
            })
            response = self._request("GET", f"/checkouts/{checkout.id}/download?{query}", allow=(200,))
            payload = response.json()
            url = payload.get("redirect") or payload.get("url")
            if not url:
                raise OdiloError(f"no file location in the {wanted} response: {str(payload)[:120]}")
            blob = self._request("GET", url, auth=False, allow=(200,)).content
        folder = pathlib.Path(dest_dir) if dest_dir else self.settings.download_dir
        folder.mkdir(parents=True, exist_ok=True)
        stamp = checkout.issue_date or time.strftime("%Y%m%d")
        suffix = ".acsm" if is_acsm(blob) else (".pdf" if blob[:4] == b"%PDF" else ".bin")
        path = folder / f"{checkout.record_id}-{stamp}-{wanted.lower()}{suffix}"
        path.write_bytes(blob)
        return path


def is_acsm(blob: bytes) -> bool:
    """True when the payload is an Adobe fulfilment token rather than a book."""
    head = blob[:2048]
    return b"<fulfillmentToken" in head or b"adept" in head.lower()
