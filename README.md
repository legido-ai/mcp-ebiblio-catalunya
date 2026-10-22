# mcp-ebiblio-catalunya

MCP server for **eBiblio Catalunya**, the Catalan public-library e-lending
service. It borrows and downloads newspaper and magazine issues.

Andalucía's eBiblio runs on **Cantook**; Catalunya runs on **Odilo**. They share
no endpoint, no session model and no catalogue ids, so this is a separate server
rather than another deployment of `mcp-ebiblio`. What they do share is the loan
format: an **ACSM** token that `libgourou` fulfils and deprotects.

## What it does

- find a publication by title, ignoring accents and case (`El País` = `el pais`)
- list the issues a publication currently offers
- borrow a dated issue (`YYYYMMDD` or `YYYY-MM-DD`, default: the newest)
- download it and deprotect it to PDF/EPUB
- list the loans held, and return one when the platform allows it

## The flow, and why it looks like this

Every shape below was read out of the platform's own JavaScript bundle and then
verified against the live service:

```
POST /opac/api/v2/token            grant_type=client_credentials  (HTTP Basic)
POST /opac/api/v2/login            userId=<card>&password=<pin>   -> cookies + patron id
GET  /opac/api/v2/records?query=…  -> {"records": [...], "total": N}
POST /opac/api/v2/records/{rid}/checkout?patronId=&from=RECORD_SCREEN&issueDate=YYYYMMDD
GET  {checkout.downloadUrl}        -> application/vnd.adobe.adept+xml (ACSM)
GET  /opac/api/v2/patrons/{pid}/checkouts
POST /opac/api/v2/checkouts/{cid}/return        patronId=
```

Three traps are worth knowing before touching this code:

1. **The `Accept` header must be the SPA's own value**
   (`application/json, text/plain, */*`). Without it `/login` answers **HTTP
   500** for every credential — real or dummy — which reads as a broken account
   or a dead endpoint. With it you get an honest `401 {"errorCode": ...}`.
2. **Results live under `records`, not `data`.** Reading `.data` yields an empty
   list and no error, so a search "works" and finds nothing.
3. **There is no `context` parameter.** Inventing one (a natural guess for an
   OPAC API) produces `400 Context not valid`.

Also: the query string of the checkout call is *also* its form body — Odilo's
house style throughout the API. And the per-checkout `token` inside
`downloadUrl` is the only token the download accepts; the client token gives
`400 ["Invalid checkout token …"]`.

## Formats, and the five-minute window

The same loan offers three download formats, and they are not interchangeable:

- **ACSM** — the Adobe fulfilment token (~1.3 KB). This is the route that yields
  a file, but the token **expires in about five minutes**, so it must be
  fulfilled immediately (`acsmdownloader` + `adept_remove` via libgourou). The
  result is the complete issue: 21.8 MB, 56 pages for El País. Fetching the ACSM
  also makes the loan non-returnable.
- **OCS** — a ~20 MB PDF that is, despite the name, **AES-256 encrypted** with an
  Odilo policy key (`/V 5 /R 6 /CFM /AESV3`, empty password refused); only their
  reader opens it. The loan stays returnable.
- **PDF** — an HTML reader wrapper (`nubereader-pdfs.odilotk.es`), not a file.

A job that wants the paper takes the ACSM and fulfils it in the same breath.
A caller that only wants to read and hand the loan back takes OCS.

## Returns

Loans are returnable, and there are two routes — the difference matters:

- **Library API**: `POST /checkouts/{cid}/return` (body/query `patronId=`). It
  returns `200` and the loan disappears for a loan whose file was never
  fetched. Once the **ACSM** has been served, the platform sets
  `returnable = false` (verbatim from its bundle:
  `downloadFormat.includes("ACSM") && (…returnable = false)`) and answers `400`.
- **Adobe loan protocol**: the return that always works. `acsmdownloader`
  serialises a signed loan token into `<adept>/loans/<hash>.xml`, and libgourou's
  loan manager hands it back to the vendor's fulfilment server:

  ```sh
  adept_loan_mgt --adept-directory <adept> --list
  #        ID            Book          Expiration
  #  3c88253a854d7c34    El Paso       (Expired)
  printf 'y\n' | adept_loan_mgt -D <adept> -r 3c88253a854d7c34
  #  Loan 3c88253a854d7c34 successfully returned
  ```

  **The invocation matters, and the obvious one is wrong**: the long form
  (`--return <id>`) exits **0**, prints nothing and returns nothing at all — the
  loan is still in the list afterwards. Only the short `-r <id>` returns it, and
  because `-r` asks for confirmation, `y` has to be written to stdin; otherwise
  the tool reads EOF and does nothing. Verified: after the real return,
  `/patrons/{pid}/checkouts` was **empty** — the library account was freed.
  `fetch_issue` does this automatically after deprotecting (see
  `returned_loan_id` in its result), and `AdeptToolchain.return_latest()` is the
  helper. Expiry in the listing does not prevent the return, and `-r`
  needs the loan id as an argument.

So: fulfil the ACSM → `adept_remove` → plain PDF (no format conversion needed) →
return the loan through the Adobe protocol. Nothing is left on the account.

Newspaper loans last **60–120 minutes** and are renewable (`POST
/checkouts/{cid}/renew`, body `patronId=`; it returns a fresh checkout with its
own `downloadUrl`; `409 Checkout not renewable` once used).

## Install and run

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

# credentials, from the environment only
export CATALUNYA_EBIBLIO_USERNAME=...   # card number
export CATALUNYA_EBIBLIO_PASSWORD=...   # PIN
export PYTHONPATH=src
python -m odilo_ebiblio                 # MCP over stdio
```

`libgourou` (`adept_activate`, `acsmdownloader`, `adept_remove`) must be on the
PATH for deprotection; without it the ACSM is still fetched and returned.

## Tools

- `find_publication(title, kind="newspaper")` — record id, issues available, newest issue
- `list_issues(title, limit=14)` — issues, newest first
- `borrow_issue(title, issue="")` — borrow (reuses a loan already held)
- `fetch_issue(title, issue="", deprotect=True)` — borrow, download, deprotect; the job-facing tool
- `list_loans()`, `return_loan(checkout_id)`, `account()`

## Tests

```sh
make test        # hermetic: no network, no credentials
```

The suite drives the client through an injected transport, asserting the exact
call shapes above — the URL, the body, the headers and the parsing keys.

## Configuration

| variable | meaning |
| --- | --- |
| `CATALUNYA_EBIBLIO_USERNAME` / `_PASSWORD` | card and PIN (or the generic `EBIBLIO_USERNAME` / `EBIBLIO_PASSWORD`) |
| `EBIBLIO_BASE_URL` | default `https://biblioteca.ebiblio.cat` |
| `EBIBLIO_DATA_DIR` | downloads and the ADEPT device (default `/data`) |
| `EBIBLIO_LIBGOUROU_BIN_DIR` | where the libgourou binaries live |
| `EBIBLIO_TIMEOUT` | per-request timeout in seconds |

No secret belongs in the tree; `.env` is ignored and `.env.example` documents
every variable.
