import os
import sys
import tempfile
from pathlib import Path

# Must be set before the app is imported.
os.environ["TRENDPACK_DEMO"] = "1"
os.environ["TRENDPACK_DATA"] = tempfile.mkdtemp(prefix="trendpack-test-")
os.environ["APP_PASSWORD"] = "test-password"
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("TAVILY_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
