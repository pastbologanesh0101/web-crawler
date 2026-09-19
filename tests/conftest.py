import os
import sys

# Ensure the repo root (which holds crawl.py and the testsite package) is
# importable regardless of the directory pytest is invoked from.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
