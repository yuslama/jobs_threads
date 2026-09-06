"""The Source seam.

Two sources today. Instagram is a plausible third (PRD section 7 says build
this seam anyway), so everything a source must expose lives here and the
pipeline never learns which one it is talking to.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..parser import Post


@dataclass
class SourceResult:
    """What one collection cycle produced."""

    source: str
    posts: list[Post] = field(default_factory=list)
    #: Things that went wrong but did not abort the cycle (a private profile,
    #: a dead post URL). A cycle with errors and posts is still a success.
    errors: list[str] = field(default_factory=list)
    #: Set when the cycle could not run at all, so the loop counts a failure.
    fatal: str | None = None
    #: True when the source stopped early on purpose (quota gone), which is a
    #: normal outcome and not a failure.
    stopped_early: bool = False
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.fatal is None


class Source(ABC):
    """A discovery source: something that yields candidate posts on a cycle."""

    name: str = "abstract"

    @property
    @abstractmethod
    def enabled(self) -> bool: ...

    @abstractmethod
    async def collect(self) -> SourceResult:
        """Run one cycle. Must not raise: report trouble in the result."""
