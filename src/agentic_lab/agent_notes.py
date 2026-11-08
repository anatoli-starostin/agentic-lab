"""What a lab tells its agents beyond the framework's own instructions: added to every
manager's and worker's system prompt, after its GitHub paragraph.

- `agent_notes` in lab.yaml: the lab's own conventions, in the admins' words (where results
  go, which team on which service, how to use a key). Free text, the same for every agent.
- The names of the member keys the agent has (member_env.py): the variables are in its
  environment, but nothing else tells it they exist. Names only, never values.
"""

MAX_NOTES = 8000            # characters: it's in every session's system prompt


def check_config(notes):
    """[(where, message)] for lab.yaml's `agent_notes`."""
    if not isinstance(notes, str):
        return [("agent_notes", "must be text (a YAML block: `agent_notes: |`)")]
    if not notes.strip():
        return [("agent_notes", "is empty: leave it out instead")]
    if len(notes) > MAX_NOTES:
        return [("agent_notes", f"is {len(notes)} characters; keep it under {MAX_NOTES} "
                                "(it's in every agent's system prompt): link to a document "
                                "for the details")]
    return []


def keys_paragraph(names):
    """The member keys the agent has, by name."""
    names = sorted(set(names))
    if not names:
        return ""
    return ("\nYour member's own keys are set in your environment (and your long runs'): "
            + ", ".join(names) + ". Use them for what their names say (the lab's notes "
            "below may say how). Never print them, and never put them in files, commits, "
            "logs, results or messages; pass them to programs through the environment.")


def instructions(lab, key_names=()):
    """The text for the system prompt: member keys, then the lab's notes."""
    text = keys_paragraph(key_names)
    notes = (lab.data.get("agent_notes") or "").strip()
    if notes:
        text += ("\n\nNotes from this lab's admins (its conventions; they don't override how "
                 "you work above):\n" + notes)
    return text
