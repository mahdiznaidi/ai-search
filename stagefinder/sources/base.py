from __future__ import annotations

from typing import Protocol

from stagefinder.models import RawJob


class Source(Protocol):
    name: str

    def supports(self, country: str) -> bool: ...

    def search(self, keyword: str, location: str, country: str, job_type: str | None,
               hours_old: int | None, is_remote: bool, limit: int) -> list[RawJob]: ...
