"""End-to-end check against eBiblio Catalunya, driven by the client itself.

Read-only unless ``--fetch`` is given, and even then it reuses a loan that is
already held instead of borrowing a second one (a downloaded loan cannot be
returned, so it must not be spent twice).

    oci-env CATALUNYA_EBIBLIO_USERNAME CATALUNYA_EBIBLIO_PASSWORD -- \
        .venv/bin/python scripts/e2e_catalunya.py [--fetch] [--title "El País"]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from odilo_ebiblio.client import OdiloClient, pretty_date  # noqa: E402
from odilo_ebiblio.config import Settings  # noqa: E402
from odilo_ebiblio.drm import AdeptToolchain, DrmError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", default="El País")
    parser.add_argument("--issue", default="")
    parser.add_argument("--fetch", action="store_true",
                        help="download the issue (reuses a held loan)")
    args = parser.parse_args()

    settings = Settings.from_env(os.environ)
    settings = Settings(**{**settings.__dict__, "data_dir": Path("/srv/work/catalunya-test")})
    client = OdiloClient(settings)

    who = client.login()
    print(f"1. login ok: {who.get('name')} (patron {client.patron_id})")

    record = client.find_serial(args.title)
    if record is None:
        print(f"   no catalogue record matches {args.title!r}")
        return 1
    print(f"2. record {record.id} {record.title!r} kind={record.kind} formats={record.formats}")
    issues = record.issue_dates
    print(f"3. {len(issues)} issues, newest {pretty_date(issues[0]) if issues else '-'}; "
          f"first four: {[pretty_date(d) for d in issues[:4]]}")

    day = args.issue or record.newest_issue
    held = client.checkout_for(record.id, day)
    print(f"4. issue {pretty_date(day)} already held: {bool(held)}")

    if not args.fetch:
        loans = client.checkouts()
        print(f"5. open loans: {len(loans)} {[l.to_dict() for l in loans]}")
        return 0

    checkout = held or client.borrow_issue(record.id, day)
    print(f"5. checkout {checkout.id} (borrowed now: {not held})")
    path = client.download(checkout)
    print(f"6. downloaded {path} ({path.stat().st_size} bytes)")

    toolchain = AdeptToolchain(settings.adobe_dir, settings.libgourou_bin_dir)
    try:
        final = toolchain.fulfil_acsm(path, Path(settings.download_dir) / "drm-free")
        print(f"7. deprotected -> {final} ({final.stat().st_size} bytes)")
    except DrmError as exc:
        print(f"7. deprotection unavailable here: {exc}")

    loans = client.checkouts()
    print(f"8. open loans: {len(loans)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
