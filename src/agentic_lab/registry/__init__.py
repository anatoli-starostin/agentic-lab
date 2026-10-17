"""The lab registry (`lab.yaml`): see DESIGN.md §8."""
from .lab import ROLES, Issue, Lab, check, load

__all__ = ["ROLES", "Issue", "Lab", "check", "load"]
