"""Print the PINNED table for xnb/chromium.py.

Downloads chrome-headless-shell for every Chrome for Testing platform and hashes it.

    python tools/pin_chromium.py            # latest Stable
    python tools/pin_chromium.py 154.0.8037.57
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request

CFT_JSON = "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"
LAST_GOOD = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"


def sha256_url(url: str) -> str:
    h = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120) as resp:
        for chunk in iter(lambda: resp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    if len(sys.argv) > 1:
        version = sys.argv[1]
        data = json.load(urllib.request.urlopen(CFT_JSON))
        entry = next((v for v in data["versions"] if v["version"] == version), None)
        if entry is None:
            print(f"unknown version {version}", file=sys.stderr)
            return 1
    else:
        entry = json.load(urllib.request.urlopen(LAST_GOOD))["channels"]["Stable"]
        version = entry["version"]
    downloads = entry["downloads"].get("chrome-headless-shell", [])
    print(f'VERSION = "{version}"')
    print("PINNED = {")
    for d in downloads:
        print(f'    "{d["platform"]}": "{sha256_url(d["url"])}",', flush=True)
    print("}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
