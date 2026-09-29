"""Build the TypeScript web bundle (web/ -> xnb/_web) before packaging."""

import os
import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class WebBundleHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version, build_data):
        root = Path(self.root)
        out = root / "xnb" / "_web"
        web = root / "web"
        if os.environ.get("XNB_SKIP_WEB_BUILD") and (out / "index.html").exists():
            return
        if not web.exists():
            if (out / "index.html").exists():
                return  # building from an sdist that already contains the bundle
            raise RuntimeError("web/ sources missing and no prebuilt xnb/_web bundle")
        npm = shutil.which("npm")
        if npm is None:
            if (out / "index.html").exists():
                self.app.display_warning("npm not found; using the existing xnb/_web bundle")
                return
            raise RuntimeError("npm is required to build the web bundle")
        if not (web / "node_modules").exists():
            subprocess.run([npm, "ci" if (web / "package-lock.json").exists() else "install"], cwd=web, check=True)
        subprocess.run([npm, "run", "build"], cwd=web, check=True)
