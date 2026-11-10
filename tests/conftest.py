"""pytest autouse hook: block non-localhost network for every test."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import network_guard  # noqa: E402

network_guard.install()
