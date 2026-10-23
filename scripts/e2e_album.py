"""End-to-end album check against eBiblio Catalunya.

Borrows one album, returns the loan, downloads its tracks and prints what it
took. It spends a loan, so it is meant to be run deliberately:

    oci-env CATALUNYA_EBIBLIO_USERNAME CATALUNYA_EBIBLIO_PASSWORD -- \\
        .venv/bin/python scripts/e2e_album.py --query Bach
    ... --record 00811930 --dest /srv/work/mcp-ebiblio-catalunya-data/music
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from odilo_ebiblio.client import OdiloClient  # noqa: E402
from odilo_ebiblio.config import Settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="Bach")
    parser.add_argument("--record", default="")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--dest", default="")
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args()

    settings = Settings.from_env(os.environ)
    client = OdiloClient(settings)
    who = client.login()
    print(f"1. login ok: {who.get('name')} (patron {client.patron_id})")

    if args.record:
        record = client.record(args.record)
    else:
        record = client.find_album(args.query)
    if record is None:
        print(f"   no album matches {args.query!r}")
        return 1
    print(f"2. album {record.id} {record.title!r} by {record.author} "
          f"({record.raw.get('publisher')}, {record.year}) subjects={record.subjects[:3]}")

    print(f"3. albums for {args.query!r}:")
    for candidate in client.search_albums(args.query)[:args.limit]:
        print(f"   [{candidate.id}] {candidate.title[:60]!r} — {candidate.author} ({candidate.year})")
    if args.list_only:
        return 0

    open_before = [l.to_dict() for l in client.checkouts()]
    print(f"4. open loans before: {open_before}")

    album = client.fetch_album(record.id, dest_dir=Path(args.dest) if args.dest else None)
    print(f"5. tracks: {album['track_count']} ({album['bytes'] / 1e6:.1f} MB) in {album['album_dir']}")
    for track in album["tracks"]:
        print(f"   {track['file']}  {track['duration_ms'] / 1000:7.1f}s  {track['bytes']:>9} bytes")
    print(f"6. loan returned before downloading: {album['returned']} (checkout {album['checkout_id']})")
    print(f"7. open loans after: {[l.to_dict() for l in client.checkouts()]}")
    return 0 if album["returned"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
