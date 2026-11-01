"""Borrow one catalogue record, produce a DRM-free EPUB + MOBI, return the loan.

    docker run --rm --user 0:0 \
      -v /srv/work/mcp-ebiblio-catalunya/src:/app/src:ro \
      -v /srv/work/mcp-ebiblio-catalunya/scripts:/probe:ro \
      -v /srv/work/mcp-ebiblio-catalunya-data:/data \
      -e PYTHONPATH=/app/src -e EBIBLIO_DATA_DIR=/data -e EBIBLIO_ADEPT_DIR=/data/.adobe \
      -e CATALUNYA_EBIBLIO_USERNAME -e CATALUNYA_EBIBLIO_PASSWORD \
      --entrypoint python mcp-ebiblio:local /probe/fetch_book.py 00115631

Why this is not a server tool: `find_publication`/`fetch_issue` are newspaper
shaped (a serial plus an issue date). A book is a plain record, and the catalogue
search is token-OR ordered by modification date, so the record id has to be found
by filtering titles client-side (see find.py) before calling this.

The ACSM must be borrowed, downloaded and fulfilled in one breath (the token
expires in ~5 minutes), and the loan has to be handed back through the Adobe
protocol: the library's own return endpoint refuses once the ACSM was served.
"""
import sys
from pathlib import Path

from odilo_ebiblio.drm import convert_ebook
from odilo_ebiblio.server import _toolchain, get_client, get_settings

rid = sys.argv[1]
client = get_client()
settings = get_settings()
rec = client.record(rid)
print("record:", rec.id, rec.title, rec.kind, rec.raw.get("formats"))

checkout = client.checkout_for(rec.id) or client.borrow_record(rec.id)
print("checkout:", checkout.id)

acsm = client.download(checkout, format="ACSM", dest_dir=settings.download_dir)
print("acsm:", acsm, Path(acsm).stat().st_size)

toolchain = _toolchain()
out = toolchain.fulfil_acsm(Path(acsm), Path(settings.download_dir) / "drm-free")
print("drm-free:", out, Path(out).stat().st_size)

# Give the loan back at the ADEPT level. `return_latest` only knows about the
# newest entry it can see, so fall back to the explicit list rather than leaving
# a loan riding out its expiry.
returned = None
try:
    returned = toolchain.return_latest()
except Exception as exc:  # noqa: BLE001 - reported, not swallowed
    print("return_latest error:", exc)
if not returned:
    for loan in toolchain.loans():
        loan_id = loan.get("id") or loan.get("Id") or ""
        if not loan_id:
            continue
        try:
            if toolchain.return_loan(loan_id):
                returned = loan_id
        except Exception as exc:  # noqa: BLE001
            print("return failed", loan_id, exc)
print("returned:", returned or "NONE - check list_loans")
print("loans left:", [c.id for c in client.checkouts()])

stem = "".join(ch if ch.isalnum() or ch in " -_." else "_" for ch in rec.title).strip()
epub = Path(settings.download_dir) / f"{stem}.epub"
epub.write_bytes(Path(out).read_bytes())
print("epub:", epub, epub.stat().st_size)
mobi = convert_ebook(epub, "mobi", Path(settings.download_dir) / f"{stem}.mobi")
print("mobi:", mobi, Path(mobi).stat().st_size)
