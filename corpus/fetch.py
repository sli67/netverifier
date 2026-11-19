#!/usr/bin/env python3
"""Download the reference documents listed in sources.json into raw/.

Usage: python3 corpus/fetch.py [--force]

Existing files are kept unless --force is given. Every run rewrites
manifest.json with the size and SHA-256 of each file present. Sources marked
"manual" cannot be downloaded by a script and are only checked for presence.
The downloaded files are vendor-copyrighted, so raw/ is not committed.
"""

import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) docs-fetch"


def looks_valid(path: str, data: bytes) -> bool:
    """Reject bot-challenge and error pages saved in place of the document."""
    if path.endswith(".pdf"):
        return data.startswith(b"%PDF")
    if b"<title>Client Challenge</title>" in data[:4096]:
        return False
    return len(data) > 2048


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def main() -> int:
    force = "--force" in sys.argv[1:]
    sources = json.loads((ROOT / "sources.json").read_text())["sources"]
    manifest, problems = [], []

    for source in sources:
        target = RAW / source["path"]
        status = "present"

        if source.get("manual"):
            if not target.exists():
                problems.append(f"{source['path']}: download by hand from {source['url']}")
                continue
        elif force or not target.exists():
            try:
                data = download(source["url"])
            except Exception as error:  # noqa: BLE001 - report and continue with the rest
                problems.append(f"{source['path']}: {error}")
                continue
            if not looks_valid(source["path"], data):
                problems.append(f"{source['path']}: response was not the expected document")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            status = "downloaded"

        data = target.read_bytes()
        if not looks_valid(source["path"], data):
            problems.append(f"{source['path']}: file on disk is not the expected document")
            continue
        fetched_at = datetime.fromtimestamp(target.stat().st_mtime, timezone.utc)
        manifest.append(
            {
                **{k: source.get(k) for k in ("path", "vendor", "os", "version", "title", "url")},
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "fetchedAt": fetched_at.isoformat(timespec="seconds"),
            }
        )
        print(f"{status:10} {len(data):>10}  {source['path']}")

    (ROOT / "manifest.json").write_text(json.dumps({"files": manifest}, indent=2) + "\n")
    print(f"\n{len(manifest)} of {len(sources)} sources present; manifest.json written")
    for problem in problems:
        print(f"MISSING  {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
