"""Standalone Grok / x.ai protocol registration CLI.

Separated from grokcli-2api registration sidecar (grok-build-auth + adapter).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure vendored xconsole_client (repo root) is importable.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

__version__ = "1.0.0"
__all__ = ["__version__"]
