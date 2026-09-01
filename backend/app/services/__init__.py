"""Service-layer modules for Haunter.

The `billing` module exists to give the Phase 6 demo canonical test
(`backend/tests/demo_canonical/test_demo_canonical.py`) a real import
target. The demo test imports `charge` from `app.servies.billing` (a
deliberate one-character typo) so that the orchestrator's deterministic
ModuleNotFoundError fast-path (Phase 3) has a reproducible failure to fix.
"""
