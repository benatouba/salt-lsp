"""Intentional side effect: only the positive-control test may execute this."""

import os
from pathlib import Path

Path(os.environ["PROOF_SENTINEL"]).write_text("workspace code executed")
