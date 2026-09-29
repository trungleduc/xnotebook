"""Stamp the release version into xnotebook/__init__.py (called by semantic-release)."""

import re
import sys
from pathlib import Path

version = sys.argv[1]
if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-.+][0-9A-Za-z.\-]+)?", version):
    sys.exit(f"invalid version: {version}")
init = Path(__file__).resolve().parent.parent / "xnotebook" / "__init__.py"
text, n = re.subn(r'^__version__ = ".*"$', f'__version__ = "{version}"', init.read_text(), flags=re.M)
if n != 1:
    sys.exit("__version__ not found in xnotebook/__init__.py")
init.write_text(text)
print(f"xnotebook {version}")
