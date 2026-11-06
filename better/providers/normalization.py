import re
import unicodedata


def normalized_name(name: str) -> str:
    value = unicodedata.normalize("NFKD", name).casefold()
    return re.sub(r"\s+", " ", "".join(c for c in value if not unicodedata.combining(c))).strip()


class IdentityRegistry:
    """Explicit aliases only: no ambiguous fuzzy matching of football identities."""

    def __init__(self):
        self._aliases: dict[tuple[str, str], str] = {}

    def register(self, kind: str, internal_id: str, *aliases: str):
        keys = [(kind, normalized_name(alias)) for alias in aliases]
        if any(key in self._aliases and self._aliases[key] != internal_id for key in keys):
            raise ValueError("alias maps to conflicting identities")
        for key in keys:
            self._aliases[key] = internal_id

    def resolve(self, kind: str, external_name: str) -> str:
        return self._aliases[(kind, normalized_name(external_name))]


def numeric_source_conflict(values: dict[str, float], tolerance: float) -> dict | None:
    if tolerance < 0:
        raise ValueError("tolerance must be nonnegative")
    if values and max(values.values()) - min(values.values()) > tolerance:
        return {"type": "SOURCE_CONFLICT", "values": values, "tolerance": tolerance}
    return None
