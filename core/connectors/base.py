from __future__ import annotations

from typing import Protocol

from ..schema import EvidenceRecord


class SearchAgentError(RuntimeError):
    pass


class MissingCredentialError(SearchAgentError):
    pass


class SearchClient(Protocol):
    provider: str

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        start: int = 1,
        query_intent: str | None = None,
    ) -> list[EvidenceRecord]:
        ...


def require_secret(value: str | None, label: str) -> str:
    if not value:
        raise MissingCredentialError(f"{label} is missing")
    return value
