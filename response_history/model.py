from dataclasses import dataclass, field


@dataclass
class Turn:
    provider: str
    source: str
    identity: str | None = None
    parts: list[str] = field(default_factory=list)
    state: str = "active"
    evidence: str | None = None
    excluded: str | None = None
    prompt: str = ""  # the real human prompt(s) that began this turn

    @property
    def selectable(self) -> bool:
        return self.state == "complete" and not self.excluded and bool(self.parts)

    @property
    def text(self) -> str:
        return "\n\n".join(self.parts)


class TranscriptError(ValueError):
    pass


class UserError(ValueError):
    """A mistake in what was typed, or a refusal; its message is written for the person and is safe to show."""
