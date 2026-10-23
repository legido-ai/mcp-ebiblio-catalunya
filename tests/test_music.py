"""The album (music) flow: what is borrowed, what is returned, and in what order.

The order asserted here is not a style choice. Measured against the live service:
an audio loan that is still open when a track is downloaded can never be returned
(the platform answers HTTP 400 for its whole 21-day term), while the signed track
URLs keep working after the loan is handed back. So borrow, take the player link,
**return**, and only then download.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FakeResponse, FakeSession
from odilo_ebiblio.client import (
    Checkout,
    OdiloClient,
    RequestFailure,
    safe_filename,
    track_label,
)

ALBUM_JSON = {
    "id": "00756144",
    "title": "Nocturns per a piano",
    "author": "Sira Hernández",
    "publisher": "La mà de guido",
    "publicationDate": "20120101",
    "language": "cat",
    "coverImageUrl": "https://covers.odilo.io/x.jpg",
    "subjects": ["Música", "Música de producció catalana (Km0"],
    "resourceTypes": ["TIPO_AUDIO", "TIPO_OCS_AUDIO", "TIPO_ALBUM"],
    "formats": ["MP3", "AUDIO_ENCRYPTED"],
}

PLAYER_URL = ("https://player.eu.odilo.io/?mediaId=6a0ed0011760d319633a7a45"
              "&keyId=6adb8bd65ba05a160415fdf4&lang=en")

MANIFEST = {
    "resources": [
        {"type": "Cloudfront", "url": "https://cdn/x/01.mp3?sig=a",
         "title": "01. Nocturno n.2 en do menor", "format": "AUDIO", "duration": 261015,
         "object": "OdiloPlace_APECAT_INTERNO/dr1/01. Nocturno n.2 en do menor.mp3"},
        {"type": "Cloudfront", "url": "https://cdn/x/02.mp3?sig=b",
         "title": "02. Nocturno op.48 n.1 en do menor", "format": "AUDIO", "duration": 474017,
         "object": "OdiloPlace_APECAT_INTERNO/dr1/02. Nocturno op.48 n.1 en do menor.mp3"},
    ],
    "metadata": {"title": "Nocturns per a piano", "previewUrl": "preview/x/01.mp3"},
}

CHECKOUT = {
    "id": "354426909", "recordId": "00756144",
    "downloadUrl": "https://biblioteca.ebiblio.cat/opac/api/v2/checkouts/354426909/download"
                   "?patronId=360054490&token=76cf4e51d63d5cba",
    "returnable": True, "expired": False, "formats": ["MP3", "AUDIO_ENCRYPTED"],
    "resourceType": "TIPO_OCS_AUDIO;TIPO_AUDIO", "mediaId": "6a0ed0011760d319633a7a45",
}


def album_session(token_response) -> FakeSession:
    """A logged-in session whose answers are the shapes the platform really sends."""
    return FakeSession([
        ("/token", token_response),
        ("/login", FakeResponse(200, json_body={"id": "360054490", "name": "Card Holder"})),
        ("/records?limit", FakeResponse(200, json_body={"total": 3, "records": [
            ALBUM_JSON,
            {"id": "00824334", "title": "Todo lo que amamos", "resourceTypes": ["TIPO_AUDIO", "TIPO_OCS_AUDIO"],
             "formats": ["MP3", "AUDIO_ENCRYPTED"], "subjects": ["Ficción moderna y contemporánea"]},
            {"id": "00783853", "title": "La sinfonía de la libertad", "resourceTypes": ["TIPO_EBOOK"],
             "formats": ["ACSM"], "subjects": ["Música"]},
        ]})),
        ("/records/00756144/checkout?", FakeResponse(200, json_body=CHECKOUT)),
        ("/patrons/360054490/checkouts", FakeResponse(200, json_body=[])),
        ("/checkouts/354426909/download?patronId", FakeResponse(200, json_body={"redirect": PLAYER_URL})),
        ("/checkouts/354426909/return?", FakeResponse(200, json_body={})),
        ("/api/v1/media/", FakeResponse(200, json_body=MANIFEST)),
        ("https://cdn/x/01.mp3", FakeResponse(200, content=b"ID3" + b"\x00" * 200, text="")),
        ("https://cdn/x/02.mp3", FakeResponse(200, content=b"ID3" + b"\x00" * 400, text="")),
        ("/records/00756144", FakeResponse(200, json_body=ALBUM_JSON)),
    ])


def order(session: FakeSession, fragment: str) -> int:
    for index, call in enumerate(session.calls):
        if fragment in call["url"]:
            return index
    raise AssertionError(f"no call matching {fragment!r} in {[c['url'] for c in session.calls]}")


def test_album_kind_is_the_album_marker_not_the_audio_one():
    from odilo_ebiblio.client import Record

    album = Record(id="1", title="A", raw={"resourceTypes": ["TIPO_AUDIO", "TIPO_OCS_AUDIO", "TIPO_ALBUM"],
                                           "subjects": ["Música"]})
    audiobook = Record(id="2", title="B", raw={"resourceTypes": ["TIPO_AUDIO", "TIPO_OCS_AUDIO"],
                                               "subjects": ["Ficción moderna y contemporánea"]})
    assert album.kind == "album" and album.is_music
    assert audiobook.kind == "audiobook"


def test_search_albums_drops_audiobooks_and_books_about_music(settings, token_response):
    session = album_session(token_response)
    client = OdiloClient(settings, transport=session)
    found = client.search_albums("Bach")
    assert [r.id for r in found] == ["00756144"]
    assert found[0].author == "Sira Hernández"
    assert found[0].year == 2012
    assert found[0].subjects[0] == "Música"


def test_find_album_prefers_an_exact_title_then_falls_back_to_the_first(settings, token_response):
    session = album_session(token_response)
    client = OdiloClient(settings, transport=session)
    assert client.find_album("Nocturns per a piano").id == "00756144"
    assert client.find_album("nocturns per a piano").id == "00756144"   # accents/case ignored
    assert client.find_album("piano").id == "00756144"                   # no exact match: first album


def test_borrow_record_sends_no_issue_date(settings, token_response):
    session = album_session(token_response)
    client = OdiloClient(settings, transport=session)
    client.login()
    client.borrow_record("00756144")
    call = session.called("/checkout?")
    assert call["method"] == "POST"
    assert call["data"] == "patronId=360054490&from=RECORD_SCREEN"
    assert "issueDate" not in call["url"]


def test_mp3_player_uses_the_per_checkout_token_and_reads_media_and_key(settings, token_response):
    session = album_session(token_response)
    client = OdiloClient(settings, transport=session)
    client.login()
    player = client.mp3_player(Checkout.from_json(CHECKOUT))
    assert player == {"player_url": PLAYER_URL, "media_id": "6a0ed0011760d319633a7a45",
                      "key_id": "6adb8bd65ba05a160415fdf4"}
    call = session.called("/download?patronId")
    assert "format=MP3" in call["url"] and "token=76cf4e51d63d5cba" in call["url"]


def test_fetch_album_returns_the_loan_before_downloading_anything(settings, token_response, tmp_path):
    """The invariant the whole tool exists for."""
    session = album_session(token_response)
    client = OdiloClient(settings, transport=session)
    album = client.fetch_album("00756144", dest_dir=tmp_path / "album")

    borrowed = order(session, "/checkout?")
    player_link = order(session, "/download?patronId")      # the MP3 player link, taken while the loan is live
    returned = order(session, "/return?")
    manifest = order(session, "/api/v1/media/")             # the tracks, asked for after the return
    first_track = order(session, "https://cdn/x/01.mp3")
    second_track = order(session, "https://cdn/x/02.mp3")
    assert borrowed < player_link < returned < manifest < first_track < second_track
    assert album["returned"] is True
    assert album["checkout_id"] == "354426909"


def test_fetch_album_writes_plain_tracks_and_a_manifest(settings, token_response, tmp_path):
    """The album owns a directory named after its record, under the base given."""
    session = album_session(token_response)
    client = OdiloClient(settings, transport=session)
    base = tmp_path / "album"
    album = client.fetch_album("00756144", dest_dir=base)
    dest = base / "00756144"
    assert album["album_dir"] == str(dest)

    files = sorted(p.name for p in dest.glob("*.mp3"))
    assert files == ["01 - Nocturno n.2 en do menor.mp3", "02 - Nocturno op.48 n.1 en do menor.mp3"]
    assert album["track_count"] == 2
    assert album["bytes"] == sum((dest / name).stat().st_size for name in files)
    assert [t["title"] for t in album["tracks"]] == [
        "Nocturno n.2 en do menor", "Nocturno op.48 n.1 en do menor"]
    assert album["tracks"][0]["duration_ms"] == 261015
    stored = json.loads((dest / "album.json").read_text())
    assert stored["source"] == "ebiblio-catalunya"
    assert stored["record_id"] == "00756144" and stored["year"] == 2012
    assert stored["title"] == "Nocturns per a piano" and stored["author"] == "Sira Hernández"
    # no half-written file may survive a run
    assert not list(dest.glob("*.part"))


def test_a_second_fetch_of_the_same_album_borrows_nothing(settings, token_response, tmp_path):
    """A re-run must not spend a loan: the tracks are already on disk."""
    session = album_session(token_response)
    client = OdiloClient(settings, transport=session)
    base = tmp_path / "album"
    client.fetch_album("00756144", dest_dir=base)
    before = len(session.calls)
    again = client.fetch_album("00756144", dest_dir=base)
    assert again["already_fetched"] is True and again["track_count"] == 2
    fresh = [c["url"] for c in session.calls[before:]]
    assert not [url for url in fresh if "/checkout?" in url or "/download?patronId" in url]
    assert not [url for url in fresh if "cdn/x/" in url]


def test_a_failed_track_download_leaves_no_partial_file(settings, token_response, tmp_path):
    session = album_session(token_response)
    # The CDN answers 403 without the player's Referer; this entry answers first.
    session.script.insert(0, ("https://cdn/x/01.mp3", FakeResponse(403, text="denied")))
    client = OdiloClient(settings, transport=session)
    base = tmp_path / "album"
    with pytest.raises(RequestFailure):
        client.fetch_album("00756144", dest_dir=base)
    assert not list((base / "00756144").glob("*.part"))
    assert not list((base / "00756144").glob("*.mp3"))


def test_tracks_are_requested_with_the_player_as_referer(settings, token_response, tmp_path):
    """Without this header the CDN answers 403 for every track."""
    session = album_session(token_response)
    client = OdiloClient(settings, transport=session)
    client.fetch_album("00756144", dest_dir=tmp_path / "album")
    call = session.called("https://cdn/x/01.mp3")
    assert call["headers"]["Referer"] == PLAYER_URL


def test_track_labels_drop_the_order_prefix_and_filenames_stay_safe():
    assert track_label("01. Nocturno n.2 en do menor") == "Nocturno n.2 en do menor"
    assert track_label("12 - Something/Else") == "Something/Else"
    assert track_label("") == "Untitled"
    assert safe_filename("a/b:c") == "a-b-c"
    assert safe_filename("   ") == "track"
