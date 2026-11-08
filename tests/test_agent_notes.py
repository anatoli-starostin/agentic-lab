"""What a lab tells its agents (agent_notes.py): lab.yaml's `agent_notes` and the names of
the member keys an agent has, in every manager's and worker's system prompt."""
import datetime as dt
import shutil
from pathlib import Path

import pytest
import yaml

from agentic_lab import agent_notes as AN
from agentic_lab import registry

EXAMPLE = Path(__file__).parent.parent / "examples" / "minimal"
TODAY = dt.date(2026, 10, 17)
SRC = Path(__file__).parent.parent / "src" / "agentic_lab"


@pytest.fixture
def lab_dir(tmp_path):
    d = tmp_path / "lab"
    shutil.copytree(EXAMPLE, d)
    return d


def load(lab_dir, notes=None):
    p = lab_dir / "lab.yaml"
    data = yaml.safe_load(p.read_text())
    if notes is not None:
        data["agent_notes"] = notes
    p.write_text(yaml.safe_dump(data))
    return registry.load(p)


def errors(lab):
    return [str(i) for i in registry.check(lab, TODAY) if i.level == "error"]


def test_notes_are_optional(lab_dir):
    lab = load(lab_dir)
    assert errors(lab) == []
    assert AN.instructions(lab) == ""


def test_notes_and_keys_in_the_prompt(lab_dir):
    lab = load(lab_dir, "Results go to the bucket team-data.\nW&B team: example.\n")
    assert errors(lab) == []
    text = AN.instructions(lab, ["WANDB_TEAM_KEY", "DATA_KEY_ID", "DATA_KEY_ID"])
    assert "DATA_KEY_ID, WANDB_TEAM_KEY." in text            # sorted, once each
    assert "Never print them" in text
    assert text.rstrip().endswith("W&B team: example.")
    assert text.index("WANDB_TEAM_KEY") < text.index("Results go to")


def test_keys_without_notes_and_notes_without_keys(lab_dir):
    assert "Notes from" not in AN.instructions(load(lab_dir), ["K"])
    text = AN.instructions(load(lab_dir, "Use the bucket."), [])
    assert "keys" not in text and "Use the bucket." in text


def test_values_never_reach_the_prompt(lab_dir):
    """The services pass names only (sorted(env)); the text has no place for values."""
    lab = load(lab_dir, "notes")
    env = {"DATA_SECRET": "s3cr3t-value"}
    assert "s3cr3t-value" not in AN.instructions(lab, sorted(env))


@pytest.mark.parametrize("notes, msg", [
    (["a", "list"], "must be text"),
    ({"a": 1}, "must be text"),
    ("   \n", "is empty"),
    ("x" * (AN.MAX_NOTES + 1), "keep it under"),
])
def test_bad_notes_are_errors(lab_dir, notes, msg):
    errs = errors(load(lab_dir, notes))
    assert any("agent_notes" in e and msg in e for e in errs), errs


@pytest.mark.parametrize("service", ["worker/service.py", "manager/service.py"])
def test_services_add_notes_after_github(service):
    """Both kinds of agents get them, the manager in both of its entry points (Slack and
    console); member keys are read before the lab's own variables are added."""
    src = (SRC / service).read_text()
    n = src.count("ME.read(STATE)")
    assert n >= 1
    assert src.count("member_keys = sorted(env)") == n
    assert src.count("GH.instructions(lab, agent_id)\n") == n
    assert src.count("+ AN.instructions(lab, member_keys)") == n
    for block in src.split("ME.read(STATE)")[1:]:
        assert block.index("member_keys = sorted(env)") < block.index("CLAUDE_CODE_OAUTH_TOKEN")


def test_guests_never_get_the_notes():
    """A guest's turn (another member on a dedicated manager) gets the guest prompt instead
    of prompt_extra: the owner's key names stay out of it."""
    src = (SRC / "manager" / "brain.py").read_text()
    assert "if guest else (self.prompt_extra()" in src
