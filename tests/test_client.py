"""The call shapes that make the Catalunya integration work.

Each assertion mirrors a fact read out of Odilo's own bundle, because getting
any of them wrong fails in a confusing way (empty result sets, HTTP 500 on
login, "Invalid checkout token").
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FakeResponse, FakeSession
from odilo_ebiblio.client import (
    AuthenticationError,
    Checkout,
    OdiloClient,
    Record,
    RequestFailure,
    is_acsm,
    issue_date,
    normalise_title,
    pretty_date,
)
from odilo_ebiblio.config import SPA_ACCEPT


def test_token_uses_basic_auth_and_the_spa_accept_header(settings, token_response):
    session = FakeSession([("/token", token_response)])
    client = OdiloClient(settings, transport=session)
    assert client.token() == "abcdef0123456789"
    call = session.called("/token")
    assert call["method"] == "POST"
    assert call["data"] == "grant_type=client_credentials"
    assert call["headers"]["Authorization"].startswith("Basic ")
    assert call["headers"]["Accept"] == SPA_ACCEPT
    # cached: a second call must not hit the network again
    assert client.token() == "abcdef0123456789"
    assert len([c for c in session.calls if "/token" in c["url"]]) == 1


def test_login_posts_userid_and_password_and_keeps_the_patron(settings, token_response):
    session = FakeSession([
        ("/token", token_response),
        ("/login", FakeResponse(200, json_body={"id": "360054490", "name": "Card Holder"})),
    ])
    client = OdiloClient(settings, transport=session)
    client.login()
    call = session.called("/login")
    assert call["data"] == "userId=CARD&password=PIN"
    assert call["headers"]["Content-Type"].startswith("application/x-www-form-urlencoded")
    assert client.patron_id == "360054490"


def test_login_refusal_is_an_authentication_error(settings, token_response):
    session = FakeSession([
        ("/token", token_response),
        ("/login", FakeResponse(401, text='{"errorCode":"ERROR_LOGIN_UNKNOWN_ID"}')),
    ])
    client = OdiloClient(settings, transport=session)
    with pytest.raises(AuthenticationError) as err:
        client.login()
    assert "UNKNOWN_ID" in str(err.value)


def test_search_reads_the_records_key_not_data(settings, token_response):
    """Odilo answers {"records": [...]}; reading .data silently yields nothing."""
    payload = {"total": 41410, "records": [
        {"id": "00518339", "title": "El País", "resourceTypes": ["TIPO_NEWSPAPER_EBOOK"],
         "formats": ["ACSM", "PDF", "OCS"], "availableIssueDates": ["20261020", "20261022", "20261021"]},
    ]}
    session = FakeSession([("/token", token_response), ("/records?", FakeResponse(200, json_body=payload))])
    client = OdiloClient(settings, transport=session)
    found = client.search("El País")
    assert [r.id for r in found] == ["00518339"]
    record = found[0]
    assert record.kind == "newspaper"
    assert record.issue_dates == ["20261022", "20261021", "20261020"]  # newest first
    assert record.newest_issue == "20261022"
    # the query must not invent a "context" parameter: that yields HTTP 400
    assert "context=" not in session.called("/records?")["url"]


def test_find_serial_matches_accent_and_case_but_not_supplements(settings, token_response):
    payload = {"records": [
        {"id": "00518359", "title": "Suplemento El País",
         "resourceTypes": ["TIPO_NEWSPAPER_EBOOK"], "availableIssueDates": ["20261022"]},
        {"id": "00518339", "title": "El Pais",
         "resourceTypes": ["TIPO_NEWSPAPER_EBOOK"], "availableIssueDates": ["20261022"]},
    ]}
    session = FakeSession([("/token", token_response), ("/records?", FakeResponse(200, json_body=payload))])
    client = OdiloClient(settings, transport=session)
    found = client.find_serial("El País")
    assert found is not None and found.id == "00518339"
    assert normalise_title("El País") == normalise_title("el pais") == "el pais"
    assert normalise_title("Suplemento El País") != normalise_title("El País")


def test_find_serial_honours_kind(settings, token_response):
    payload = {"records": [
        {"id": "1", "title": "El País", "resourceTypes": ["TIPO_MAGAZINE_EBOOK"],
         "availableIssueDates": ["20261022"]},
    ]}
    session = FakeSession([("/token", token_response), ("/records?", FakeResponse(200, json_body=payload))])
    client = OdiloClient(settings, transport=session)
    assert client.find_serial("El País", kind="newspaper") is None
    assert client.find_serial("El País", kind="magazine") is not None


def test_borrow_issue_sends_the_documented_checkout_call(settings, token_response):
    checkout = {"id": "354081209", "recordId": "00518339", "issueDate": "20261022",
                "downloadUrl": "https://biblioteca.ebiblio.cat/opac/api/v2/checkouts/354081209/download?patronId=1&checkoutId=354081209&token=deadbeef",
                "returnable": True, "expired": False}
    session = FakeSession([
        ("/token", token_response),
        ("/login", FakeResponse(200, json_body={"id": "360054490"})),
        ("/checkout?", FakeResponse(200, json_body=checkout)),
    ])
    client = OdiloClient(settings, transport=session)
    client.login()
    result = client.borrow_issue("00518339", "2026-10-22")
    call = session.called("/checkout?")
    assert call["url"].endswith("/records/00518339/checkout?patronId=360054490&from=RECORD_SCREEN&issueDate=20261022")
    # Odilo sends the query string as the body too
    assert call["data"] == "patronId=360054490&from=RECORD_SCREEN&issueDate=20261022"
    assert result.id == "354081209"
    assert result.download_url.endswith("token=deadbeef")


def test_download_uses_the_checkout_url_and_saves_an_acsm(settings, token_response):
    blob = b"<fulfillmentToken>...</fulfillmentToken>"
    checkout = Checkout(id="1", record_id="00518339", issue_date="20261022",
                        download_url="https://biblioteca.ebiblio.cat/opac/api/v2/checkouts/1/download?token=x")
    session = FakeSession([("/token", token_response),
                           ("/checkouts/1/download", FakeResponse(200, content=blob))])
    client = OdiloClient(settings, transport=session)
    path = client.download(checkout)
    assert path.suffix == ".acsm"
    assert path.read_bytes() == blob
    assert path.name == "00518339-20261022-acsm.acsm"
    assert is_acsm(blob) and not is_acsm(b"%PDF-1.7")
    # the download carries no bearer token: the per-checkout token is the only valid one
    assert "Authorization" not in session.called("/checkouts/1/download")["headers"]


def test_download_without_url_explains_itself(settings, token_response):
    client = OdiloClient(settings, transport=FakeSession([("/token", token_response)]))
    with pytest.raises(Exception) as err:
        client.download(Checkout(id="1", record_id="2"))
    assert "downloadUrl" in str(err.value)


def test_ocs_download_follows_the_extract_url_route(settings, token_response):
    """OCS hands back a location, not the bytes: the file lives on Odilo's CDN."""
    checkout = Checkout(id="77", record_id="00518339", issue_date="20261022",
                        download_url="https://biblioteca.ebiblio.cat/opac/api/v2/checkouts/77/download"
                                     "?patronId=1&checkoutId=77&token=percheckout")
    session = FakeSession([
        ("/token", token_response),
        ("/login", FakeResponse(200, json_body={"id": "360054490"})),
        ("/checkouts/77/download", FakeResponse(200, json_body={"redirect": "https://cdn.us.odilo.io/ocs/x.ocs"})),
        ("cdn.us.odilo.io", FakeResponse(200, content=b"%PDF-1.3\n...")),
    ])
    client = OdiloClient(settings, transport=session)
    path = client.download(checkout, format="OCS")
    assert path.name == "00518339-20261022-ocs.pdf"
    assert path.read_bytes().startswith(b"%PDF")
    call = session.called("/checkouts/77/download")
    assert "format=OCS" in call["url"] and "extractUrl=yes" in call["url"]
    assert "token=percheckout" in call["url"]  # the checkout's token, never the client one


def test_checkout_token_is_read_from_download_url():
    loan = Checkout(id="1", record_id="2", download_url="https://x/download?patronId=9&token=abc123")
    assert OdiloClient.checkout_token(None, loan) == "abc123"  # type: ignore[arg-type]
    assert OdiloClient.checkout_token(None, Checkout(id="1", record_id="2")) is None  # type: ignore[arg-type]


def test_return_reports_false_when_the_loan_is_not_returnable(settings, token_response):
    session = FakeSession([
        ("/token", token_response),
        ("/login", FakeResponse(200, json_body={"id": "7"})),
        ("/return", FakeResponse(400, text="Bad Request")),
    ])
    client = OdiloClient(settings, transport=session)
    client.login()
    assert client.return_checkout("354081209") is False
    assert session.called("/return")["data"] == "patronId=7"


def test_checkout_for_matches_record_and_issue(settings, token_response):
    session = FakeSession([
        ("/token", token_response),
        ("/login", FakeResponse(200, json_body={"id": "7"})),
        ("/checkouts", FakeResponse(200, json_body=[
            {"id": "1", "recordId": "00518339", "issueDate": "20261021"},
            {"id": "2", "recordId": "00518339", "issueDate": "20261022"},
        ])),
    ])
    client = OdiloClient(settings, transport=session)
    client.login()
    assert client.checkout_for("00518339", "20261022").id == "2"
    assert client.checkout_for("00518339", "20261020") is None
    # an unknown record has no loan, and asking without a date still matches by record
    assert client.checkout_for("999") is None
    assert client.checkout_for("00518339").id == "1"


def test_non_2xx_becomes_a_request_failure(settings, token_response):
    session = FakeSession([("/token", token_response),
                           ("/records?", FakeResponse(400, text='{"detail":"Context not valid"}'))])
    client = OdiloClient(settings, transport=session)
    with pytest.raises(RequestFailure) as err:
        client.search("El País")
    assert err.value.status == 400


def test_date_helpers():
    assert issue_date("2026-10-22") == "20261022"
    assert issue_date("20261022") == "20261022"
    assert pretty_date("20261022") == "2026-10-22"
    with pytest.raises(ValueError):
        issue_date("2026-10")  # too few digits to be a day


def test_record_without_resource_types_is_other():
    assert Record(id="1", title="x").kind == "other"
