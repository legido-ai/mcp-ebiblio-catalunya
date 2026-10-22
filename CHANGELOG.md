# Changelog

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
