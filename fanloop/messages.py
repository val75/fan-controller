"""A message for the user, produced by logic modules and sent by the alerter."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    kind: str
    text: str
