"""lab slack-verify: tokens checked against Slack (faked)."""

import pytest

from agentic_lab.slack_verify import VerifyError, verify

ENV = "SLACK_BOT_TOKEN=xoxb-1\nSLACK_APP_TOKEN=xapp-1\n"


def fake_slack(socket_ok=True, bot_ok=True):
    def call(method, token, params=None):
        return {"auth.test": {"ok": bot_ok, "error": "invalid_auth", "user": "flash",
                              "user_id": "U9", "team": "Spiky", "bot_id": "B1"},
                "bots.info": {"ok": True, "bot": {"name": "Flash", "app_id": "A0CA4AXTJ65"}},
                "apps.connections.open": {"ok": socket_ok, "error": "not_allowed_token_type"},
                }[method]
    return call


def test_verify():
    r = verify("service:mgr-alice", store_get=lambda o, n: ENV, call=fake_slack())
    assert r["app_id"] == "A0CA4AXTJ65" and r["team"] == "Spiky" and r["socket_ok"]
    assert not verify("service:x", lambda o, n: ENV, fake_slack(socket_ok=False))["socket_ok"]
    with pytest.raises(VerifyError, match="bot token doesn't work"):
        verify("service:x", lambda o, n: ENV, fake_slack(bot_ok=False))
    with pytest.raises(VerifyError, match="no Slack app tokens"):
        verify("service:x", lambda o, n: None, fake_slack())

