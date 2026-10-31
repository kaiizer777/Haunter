"""Session-removed placeholder (#207).

Original tests depended on deleted session modules
(app.routers.sessions / app.services.session_orchestrator)
and had no keepable non-session coverage.
"""

import pytest


@pytest.mark.skip(reason="session removed, #207")
def test_placeholder():
    pass
