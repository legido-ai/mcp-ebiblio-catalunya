# Changelog

## 0.2.1 — 2026-11-01

**Books, not only the press.** Fetching a *book* from the Catalan catalogue is a
different shape from fetching an issue, and the loan-return path had a parser
bug that made every book loan leak.

- `scripts/find.py`: resolve a book by title. The catalogue search
  (`/records?query=`) is token-OR ordered by `fechaModifica_dt`, so a title with
  common words ("cañas y barro") returns 38 000 records with the target buried;
  a narrower query ("barro", 241 records) plus a client-side title filter is the
  reliable route. `find_publication` cannot do this: it matches serials only.
- `scripts/fetch_book.py <record_id>`: borrow → ACSM → libgourou → DRM-free
  EPUB → MOBI, returning the loan at the ADEPT level. Run it in the
  `mcp-ebiblio:local` image with `src/`, `scripts/` and the data volume mounted.
- **Fix — `parse_loan_list` dropped rows whose truncated title ends inside an
  opening parenthesis.** `adept_loan_mgt -l` cuts the title at a fixed width, so
  `Cañas y barro (Los mejores clásicos)` comes back as `Cañas y barro (Los
  mejores cl` followed by a *bare* timestamp instead of `(expiration)`. The
  parser required the last field to start with `(`, skipped the row, and
  `return_latest()` therefore believed there were no loans: every fulfilled ACSM
  stayed on the account until it expired, while `fetch_issue`/`fetch_book`
  reported success. The expiration field is now accepted bare
  (`YYYY-MM-DD…`) or wrapped, with a regression test
  (`test_parse_loan_list_accepts_a_title_truncated_inside_parentheses`).
  Measured on `Cañas y barro` (record `00115631`): the loan now returns and the
  library account goes back to empty.

## 0.2.0 — 2026-10-23

**Music albums.** The server now lends recordings, not only the press: two new
tools, `find_album` and `fetch_album`, and the client work behind them. An album
is a `TIPO_ALBUM` record with `formats: ["MP3", "AUDIO_ENCRYPTED"]` — no ACSM, so
libgourou is not involved.

The finding that shaped it: **the loan is returned before a single track is
downloaded**. With the loan open, `format=MP3` hands out a player link
(`mediaId` + single-use `keyId`) and the loan can be returned (HTTP 200) *before*
the player's API is ever asked for the tracks — the signed CloudFront URLs keep
working afterwards. Download first, and the platform marks the loan
`returnable: false` for its whole 21 days and refuses every return with HTTP 400.
Asking for `AUDIO_ENCRYPTED`/`OCS`/`ACSM` poisons a loan the same way, so the code
never asks.

- `client`: `search_albums`, `find_album`, `borrow_record` (no issue date),
  `mp3_player`, `play_manifest`, `download_track` (the CDN wants the player as
  `Referer`), `fetch_album` (borrow → player link → **return** → download →
  `album.json`), plus `Record.kind == "album"`/`"audiobook"`, `author`, `year`,
  `subjects`, `track_label`, `safe_filename`.
- `fetch_album` is idempotent: an album whose tracks are all on disk is not even
  borrowed, and an existing track is never fetched twice.
- `find_album` filters to albums, so a composer search no longer returns books
  *about* him, nor audiobooks (those carry no `TIPO_ALBUM`).
- Nine hermetic tests pin the call shapes, the returned-before-downloaded order,
  the write-out (titles, durations, `album.json`) and the failure modes (a 403
  track leaves no `.part` file behind).
- `scripts/e2e_album.py` runs the whole flow against the live service;
  `scripts/e2e_catalunya.py` still covers the press.

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
- `fetch_issue`'s served description said the loan "cannot be returned
  afterwards": corrected — it is returned through the Adobe protocol and reports
  `returned_loan_id`. The description is what a model reads before deciding
  whether to return anything, so it is corrected as carefully as the code.
- Registered in Hermes as the `ebiblio-catalunya` MCP server, so its seven tools
  (`find_publication`, `list_issues`, `borrow_issue`, `fetch_issue`, `list_loans`,
  `return_loan`, `account`) are callable directly: `config.yaml` runs
  `oci-env` (credentials from the vault, never in the file) plus `docker run` on
  the `mcp-ebiblio` image with this repository's `src/` mounted read-only, which
  is what supplies libgourou.
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
