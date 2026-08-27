"""
Root runner for Phase 4 verification script.
Delegates to backend/scripts/test_llm.py.
"""

import sys
from pathlib import Path

# Add backend to sys.path
backend_dir = Path(__file__).resolve().parent / "backend"
if not backend_dir.exists():
    backend_dir = Path(__file__).resolve().parent.parent / "backend"

sys.path.insert(0, str(backend_dir))
sys.path.insert(0, str(backend_dir / "scripts"))

from scripts.test_llm import run_all_tests
import asyncio

if __name__ == "__main__":
    asyncio.run(run_all_tests())
