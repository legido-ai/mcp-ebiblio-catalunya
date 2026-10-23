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
import json
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


#: Odilo titles track order as a numeric prefix (``"01. Nocturno n.2"``); the
#: prefix belongs in the file name (so the album sorts) and not in the label.
_TRACK_PREFIX = re.compile(r"^\s*(?:cd\s*\d+\s*[-_. ]*)?\d{1,3}\s*[-_.)]+\s*")
_UNSAFE_FILENAME = re.compile(r'[/\\:<>\*\?"\|\x00-\x1f]')


def track_label(title: str) -> str:
    """``"01. Nocturno n.2 en do menor"`` -> ``"Nocturno n.2 en do menor"``."""
    text = _TRACK_PREFIX.sub("", str(title or "")).strip(" -_.")
    return text or str(title or "").strip() or "Untitled"


def safe_filename(text: str, *, limit: int = 120) -> str:
    """A track title that is safe as a file name on any filesystem."""
    cleaned = _UNSAFE_FILENAME.sub("-", str(text or "")).strip(" .")
    return (cleaned[:limit].strip() or "track")


def _read_json(path: Any) -> dict[str, Any]:
    """The JSON at ``path``, or an empty mapping — a missing file is not an error."""
    import pathlib

    try:
        payload = json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


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
        """newspaper / magazine / album / audiobook / ... from resourceTypes.

        ``TIPO_ALBUM`` is what the Catalan catalogue marks a *music* album with;
        an audiobook carries ``TIPO_AUDIO``/``TIPO_OCS_AUDIO`` and no album marker,
        so the two never collapse into each other.
        """
        for candidate in ("newspaper", "magazine"):
            if any(candidate.upper() in t.upper() for t in self.resource_types):
                return candidate
        if any("ALBUM" in t.upper() for t in self.resource_types):
            return "album"
        if any("AUDIO" in t.upper() for t in self.resource_types):
            return "audiobook"
        return "other"

    @property
    def subjects(self) -> list[str]:
        raw = self.raw.get("subjects") or self.raw.get("subject") or []
        if isinstance(raw, str):
            return [raw]
        return [str(s) for s in raw]

    @property
    def author(self) -> str:
        return str(self.raw.get("author") or "")

    @property
    def year(self) -> int | None:
        for field in ("publicationDate", "releaseDate"):
            digits = str(self.raw.get(field) or "")
            if len(digits) >= 4 and digits[:4].isdigit():
                return int(digits[:4])
        return None

    @property
    def is_music(self) -> bool:
        """A lendable recording, as opposed to a book *about* music."""
        return self.kind == "album" or any("Música" in s or "Music" in s for s in self.subjects)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "author": self.author,
            "publisher": self.raw.get("publisher"),
            "year": self.year,
            "subjects": self.subjects[:6],
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

    def search_albums(self, query: str, *, limit: int = 24) -> list[Record]:
        """Music albums matching a query.

        A catalogue search for "Bach" mixes albums with books *about* Bach and
        with audiobooks, so the format marker is what decides: only records the
        platform labels ``TIPO_ALBUM`` are recordings.
        """
        return [r for r in self.search(query, limit=limit) if r.kind == "album"]

    def find_album(self, query: str, *, limit: int = 24) -> Record | None:
        """One album for a query: an exact title match when there is one, else the first."""
        wanted = normalise_title(query)
        candidates = self.search_albums(query, limit=limit)
        for candidate in candidates:
            if normalise_title(candidate.title) == wanted:
                return candidate
        return candidates[0] if candidates else None

    # -------------------------------------------------------------------- loans
    def borrow_issue(self, record_id: str, issue: str, *, screen: str = DEFAULT_SCREEN) -> Checkout:
        """Borrow a dated issue of a newspaper or magazine."""
        patron = self.ensure_login()
        day = issue_date(issue)
        query = f"patronId={patron}&from={screen}&issueDate={day}"
        response = self._request("POST", f"/records/{record_id}/checkout?{query}", form=query)
        return Checkout.from_json(response.json())

    def borrow_record(self, record_id: str, *, screen: str = DEFAULT_SCREEN) -> Checkout:
        """Borrow a whole record — a music album.

        A newspaper borrows a *dated issue*; an album has no issue date at all,
        and sending one earns an HTTP 400.
        """
        patron = self.ensure_login()
        query = f"patronId={patron}&from={screen}"
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

    # ------------------------------------------------------------------- albums
    def mp3_player(self, checkout: Checkout) -> dict[str, Any]:
        """The player a loan's ``MP3`` format hands out.

        An album's formats are ``MP3`` and ``AUDIO_ENCRYPTED`` — there is no ACSM,
        so libgourou has nothing to do here. ``MP3`` does not serve a file either:
        it answers with a link to Odilo's player carrying a ``mediaId`` and a
        **per-checkout keyId**, and the player exchanges that pair for the album's
        tracks. The keyId is single-use, so this is called once per run.
        """
        from urllib.parse import parse_qs, urlencode, urlparse

        token = self.checkout_token(checkout)
        if not token:
            raise OdiloError("checkout carries no downloadUrl; re-borrow or re-list loans")
        query = urlencode({
            "patronId": self.ensure_login(), "format": "MP3", "token": token,
            "redirect": "no", "extractUrl": "yes", "recordId": checkout.record_id,
        })
        response = self._request("GET", f"/checkouts/{checkout.id}/download?{query}")
        payload = response.json()
        url = payload.get("redirect") or payload.get("url")
        if not url:
            raise OdiloError(f"no player location in the MP3 response: {str(payload)[:120]}")
        params = parse_qs(urlparse(url).query)
        media_id = (params.get("mediaId") or [None])[0]
        key_id = (params.get("keyId") or [None])[0]
        if not media_id or not key_id:
            raise OdiloError(f"player link without mediaId/keyId: {url[:120]}")
        return {"player_url": url, "media_id": media_id, "key_id": key_id}

    def play_manifest(self, player: dict[str, Any]) -> dict[str, Any]:
        """The album's track list, as plain (signed) MP3 URLs.

        Verified against the live player: the resources are complete tracks at
        192 kbps, not previews — the preview is a separate object under
        ``metadata.previewUrl``.
        """
        from urllib.parse import quote

        url = (f"{self.settings.player_url.rstrip('/')}/api/v1/media/"
               f"{quote(str(player['media_id']))}/play?keyId={quote(str(player['key_id']))}")
        response = self.session.request(
            "GET", url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT,
                     "Referer": player.get("player_url") or self.settings.player_url},
            timeout=self.settings.timeout)
        if response.status_code != 200:
            raise RequestFailure(response.status_code, "GET play manifest", response.text[:200])
        return response.json()

    def download_track(self, url: str, dest: Any, *, referer: str | None = None,
                       chunk: int = 1 << 18) -> int:
        """Stream one track to disk; returns the bytes written.

        The CDN refuses a bare request — it wants the player as the referer — and
        a partially written file is never allowed to become a track.
        """
        import pathlib

        dest = pathlib.Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_suffix(dest.suffix + ".part")
        headers = {"User-Agent": USER_AGENT}
        if referer:
            headers["Referer"] = referer
        response = self.session.request("GET", url, headers=headers, stream=True,
                                        timeout=self.settings.timeout)
        if response.status_code != 200:
            raise RequestFailure(response.status_code, url, getattr(response, "text", "")[:200])
        written = 0
        try:
            with open(partial, "wb") as handle:
                stream = getattr(response, "iter_content", None)
                if callable(stream):
                    for block in stream(chunk):
                        handle.write(block)
                        written += len(block)
                else:  # a transport without streaming (the tests' fake session)
                    payload = response.content or b""
                    handle.write(payload)
                    written = len(payload)
        except (OSError, RequestFailure):
            partial.unlink(missing_ok=True)
            raise
        if written <= 0:
            partial.unlink(missing_ok=True)
            raise OdiloError(f"empty track response from {url.split('?')[0]}")
        partial.replace(dest)
        return written

    def fetch_album(self, record_id: str, *, dest_dir: Any = None,
                    reuse_loan: bool = True) -> dict[str, Any]:
        """Borrow an album, hand the loan back, then download its tracks.

        The order is the whole point, and it is the opposite of the newspaper
        flow: the MP3 player link is obtained while the loan is open, the loan is
        **returned immediately**, and only then are the (signed, still valid)
        track URLs fetched. Downloading first would leave a 21-day audio loan on
        the account that the platform refuses to take back — measured: after a
        track is downloaded, ``returnable`` is ``false`` and every return earns
        HTTP 400. Requesting ``AUDIO_ENCRYPTED``/``OCS``/``ACSM`` for an album
        poisons the loan the same way, so nothing else is ever asked for.
        """
        import pathlib

        record = self.record(record_id)
        # ``dest_dir`` is the *base*: each album owns a directory named after its
        # catalogue record, so two albums can never mix tracks.
        base = pathlib.Path(dest_dir) if dest_dir else self.settings.music_dir
        folder = base / record.id
        stored = _read_json(folder / "album.json")
        if stored and stored.get("tracks") and all(
            (folder / t.get("file", "")).exists() and (folder / t["file"]).stat().st_size > 0
            for t in stored["tracks"]
        ):
            # Already here: borrowing again would spend the account's loan for
            # bytes that are on disk, so a re-run costs nothing at all.
            total = sum(int(t.get("bytes") or 0) for t in stored["tracks"])
            return {**stored, "already_fetched": True, "album_dir": str(folder),
                    "album_file": str(folder / "album.json"), "bytes": total,
                    "track_count": len(stored["tracks"])}

        checkout = (self.checkout_for(record_id) if reuse_loan else None) or \
            self.borrow_record(record_id)
        player = self.mp3_player(checkout)
        returned = self.return_checkout(checkout.id)

        manifest = self.play_manifest(player)
        resources = [r for r in (manifest.get("resources") or []) if r.get("url")]
        folder.mkdir(parents=True, exist_ok=True)

        tracks: list[dict[str, Any]] = []
        total = 0
        for index, resource in enumerate(resources, 1):
            label = track_label(resource.get("title"))
            name = f"{index:02d} - {safe_filename(label)}.mp3"
            path = folder / name
            if path.exists() and path.stat().st_size > 0:  # a re-run re-downloads nothing
                written = path.stat().st_size
            else:
                written = self.download_track(resource["url"], path,
                                              referer=player["player_url"])
            total += written
            tracks.append({
                "file": name,
                "title": label,
                "duration_ms": int(resource.get("duration") or 0),
                "bytes": written,
                "object": resource.get("object"),
            })

        album = {
            "source": "ebiblio-catalunya",
            "record_id": record.id,
            "title": record.title,
            "author": record.author,
            "publisher": record.raw.get("publisher"),
            "year": record.year,
            "language": record.raw.get("language"),
            "subjects": record.subjects,
            "cover": record.raw.get("coverImageUrl"),
            "checkout_id": checkout.id,
            "returned": returned,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tracks": tracks,
        }
        meta_path = folder / "album.json"
        meta_path.write_text(json.dumps(album, indent=2, ensure_ascii=False) + "\n")
        return {**album, "album_dir": str(folder), "album_file": str(meta_path),
                "bytes": total, "track_count": len(tracks)}


def is_acsm(blob: bytes) -> bool:
    """True when the payload is an Adobe fulfilment token rather than a book."""
    head = blob[:2048]
    return b"<fulfillmentToken" in head or b"adept" in head.lower()
