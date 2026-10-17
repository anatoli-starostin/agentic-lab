"""Slack app manifests, generated from lab.yaml (`lab slack-manifest <agent>|approvals`).

Every agent that talks in Slack (managers, the secretary) has its own app, named after its
display_name. The approvals app ("Lab Approvals", the broker's) is deliberately minimal:
it only sends approval requests and reads replies to them.
"""
import yaml

CONVERSATIONAL_SCOPES = [
    "app_mentions:read",    # @mentions in channels
    "channels:history",     # threads it's in (public channels)
    "groups:history",       # threads it's in (private channels)
    "im:history",           # DMs
    "im:read",
    "im:write",
    "chat:write",
    "files:read",           # attachments members share: images, PDFs, data...
    "files:write",          # plots, reports, artifacts it posts back
    "reactions:write",      # 👀 while it works on a message
    "users:read",
]
CONVERSATIONAL_EVENTS = ["app_mention", "message.channels", "message.groups", "message.im"]

APPROVALS_SCOPES = ["chat:write", "im:write", "im:history"]
APPROVALS_EVENTS = ["message.im"]


def manifest(name, description, scopes, events, interactive=False, home_tab=False):
    return {
        "display_information": {"name": name[:35], "description": description[:139],
                                "background_color": "#2c2d30"},
        "features": {
            "bot_user": {"display_name": name[:80], "always_online": True},
            "app_home": {"home_tab_enabled": home_tab, "messages_tab_enabled": True,
                         "messages_tab_read_only_enabled": False},
        },
        "oauth_config": {"scopes": {"bot": list(scopes)}},
        "settings": {
            "event_subscriptions": {"bot_events": list(events)},
            "interactivity": {"is_enabled": interactive},
            "socket_mode_enabled": True,
            "org_deploy_enabled": False,
            "token_rotation_enabled": False,
        },
    }


def for_agent(lab, agent_id):
    a = lab.agents[agent_id]
    role = a.get("role")
    if role not in ("manager", "secretary"):
        raise ValueError(f"{agent_id} is a {role}: only managers and the secretary use Slack")
    what = ("a member's team member: delegates work to the lab's workers" if role == "manager"
            else "keeps the lab running: machines, agents, tokens, status")
    if role == "secretary":         # its Home tab: the lab at a glance
        return manifest(lab.display_name(agent_id), f"Agentic Lab {lab.name}: {what}",
                        CONVERSATIONAL_SCOPES, CONVERSATIONAL_EVENTS + ["app_home_opened"],
                        home_tab=True)
    return manifest(lab.display_name(agent_id), f"Agentic Lab {lab.name}: {what}",
                    CONVERSATIONAL_SCOPES, CONVERSATIONAL_EVENTS)


def approvals(lab, name="Lab Approvals"):
    return manifest(name, f"Agentic Lab {lab.name}: asks members to approve what their "
                          "agents can't do inside their sandbox.",
                    APPROVALS_SCOPES, APPROVALS_EVENTS, interactive=True)


def render(m):
    return yaml.safe_dump(m, sort_keys=False)
