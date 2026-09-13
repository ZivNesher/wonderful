import sys
from pathlib import Path

from dotenv import load_dotenv

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Picks up ANTHROPIC_API_KEY from a local .env if present, so test_live_eval.py
# runs automatically for anyone with a real key instead of always being skipped.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
