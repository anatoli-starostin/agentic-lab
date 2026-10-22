import socket

import pytest


@pytest.fixture(autouse=True)
def _sessions_unconfined_in_tests(monkeypatch):
    """No lab-claude here: sessions built in tests use Claude Code's own CLI (tests of the
    confinement call session.resolve directly)."""
    from agentic_lab.policy import session
    monkeypatch.setattr(session, "cli_path", lambda: None)


@pytest.fixture
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
