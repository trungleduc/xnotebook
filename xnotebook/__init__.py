"""xnb: run notebooks and scripts on sandboxed xeus wasm kernels inside headless Chromium."""

__version__ = "1.0.0"

from .api import CellError, RunError, run  # noqa: E402

__all__ = ["run", "CellError", "RunError", "__version__"]
