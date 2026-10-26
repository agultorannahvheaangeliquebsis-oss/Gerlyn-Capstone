class DamageReportError(Exception):
    """A refused damage-report action. str(e) is a sentence for the person who
    attempted it and is safe to show as-is."""


class DamageReportForbidden(DamageReportError):
    """The actor's role or branch does not allow this action at all."""
