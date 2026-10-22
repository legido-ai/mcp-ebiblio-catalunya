# Changelog

## 0.1.3 — 2026-10-22

**The Adobe return used the wrong invocation.** 0.1.2 called
`adept_loan_mgt --adept-directory <adept> --return <id>`, which on the live tool
**exits 0, prints nothing and returns nothing at all** — the loan stays in the
list while the code believes it was handed back. The invocation that works is the
short form, with its confirmation answered on stdin:

```sh
printf 'y\n' | adept_loan_mgt -D <adept> -r <id>
# Loan 3c88253a854d7c34 successfully returned
```

- `drm`: `return_loan` uses `-D <adept> -r <id>` and writes `y\n` to stdin; a
  silent run is now a failure rather than a success, so this cannot regress
  quietly.
- Tests pin both facts: the short form is used, and an empty output raises.
- `fetch_issue` s served description said the loan "cannot be returned afterwards":
  corrected — it is returned through the Adobe protocol and reports
  `returned_loan_id`.
- Found live, during the news mirror's first Catalunya fetch (the loan it left
  open was returned by hand, and the account was empty afterwards).

## 0.1.2 — 2026-10-22

**An ACSM loan can be returned after all** — through the Adobe protocol rather
than the library API. `acsmdownloader` serialises a signed loan token into
`<adept>/loans/<hash>.xml`; `adept_loan_mgt --list` shows it and
`adept_loan_mgt --return <id>` hands it back to the vendor
(`odilotk.es/fulfillment`), after which the library account shows **no open
loans**. Verified end to end: fulfilled El País 2026-10-22, then returned it and
`/patrons/{pid}/checkouts` was empty.

- `drm`: `AdeptToolchain.loans()`, `return_loan(loan_id)`, `return_latest()` and
  the `parse_loan_list()` parser.
- `fetch_issue` now returns the loan through the Adobe protocol right after
  deprotecting and reports `returned_loan_id`, so nothing stays on the account.
- README: both return routes documented, with the `400` case explained.

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
