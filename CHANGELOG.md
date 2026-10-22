# Changelog

## 0.1.1 — 2026-10-22

Verified the whole chain against the live service and corrected two things the
first pass got wrong:

- **Loans are returnable.** `POST /checkouts/{cid}/return` returns 200 and the
  loan disappears. What blocks it is the ACSM: the platform sets
  `returnable = false` when the download format includes ACSM, and answers 400
  afterwards. An OCS delivery keeps the loan returnable — verified end to end.
- **The three formats differ**: ACSM (the fulfilment token, expires in ~5
  minutes, must be fulfilled immediately, yields a 21.8 MB / 56-page PDF), OCS (a
  20 MB PDF that is AES-256 policy-encrypted for Odilo's reader, loan stays
  returnable) and PDF (an HTML reader wrapper).
- `download()` gained `format=`, and `checkout_token()` exposes the per-checkout
  token the download endpoint requires.
- Documented that newspaper loans last 60–120 minutes and are renewable.

## 0.1.0 — 2026-10-22

First working version, built from a live recon of the Odilo platform:

- Odilo client: client token, patron login, catalogue search, record lookup,
  issue list, dated checkout, loan list, return.
- `find_serial` matches publications on normalised titles (accents, case and
  punctuation ignored), so the same newspaper can be identified across
  libraries that use different catalogue ids.
- ACSM download through the checkout's own `downloadUrl`, plus libgourou
  fulfilment and DRM removal to PDF/EPUB.
- MCP tools: `find_publication`, `list_issues`, `borrow_issue`, `fetch_issue`,
  `list_loans`, `return_loan`, `account`.
- Hermetic test suite (13 client/config/DRM tests) driving the client through an
  injected transport.
