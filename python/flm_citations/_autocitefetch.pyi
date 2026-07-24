"""Type stubs for the `_autocitefetch` Rust extension module.

The extension is a thin binding over the `autocitefetch` library; see
`src/manager.rs` for the authoritative documentation of each method.
"""

from typing import Any, Callable, Iterable, Sequence

__version__: str

class CitationError(Exception):
    """A citation could not be retrieved or read back."""

class CiteFailure:
    """One citation that could not be retrieved."""

    @property
    def prefix(self) -> str:
        """The prefix of the citation that actually failed."""

    @property
    def key(self) -> str:
        """The key of the citation that actually failed."""

    @property
    def message(self) -> str:
        """A human-readable explanation."""

    @property
    def origin(self) -> tuple[str, str] | None:
        """The originally-requested citation this failure descends from.

        `None` when the failing citation *is* the requested one.  When an
        `arxiv:` citation chains to a DOI and that fetch fails, `prefix`/`key`
        name the DOI — which the user never wrote — and this names the arXiv
        citation they did.
        """

class CitationManager:
    """Resolves citations to CSL-JSON, backed by the Rust autocitefetch library.

    `sources` is a dict of the form `{"sources": [<spec>, ...]}`, where each
    spec is one of::

        {"kind": "arxiv",  "prefix": str, "chain_dois_to": str | None,
                           "override_dois": dict[str, str | None],
                           "override_dois_file": str}
        {"kind": "doi",    "prefix": str}
        {"kind": "manual", "prefix": str, "format": str}
        {"kind": "bib",    "prefix": str, "files": list[str],
                           "format": "auto" | "json" | "yaml", "ttl_seconds": int}
        {"kind": "bib",    "prefix": str, "entries": dict[str, Any]}
    """

    def __init__(
        self,
        sources: dict[str, Any],
        *,
        cache_dir: str | None = None,
        cache_base: str | None = None,
        user_agent: str | None = None,
        drop_csl_fields: Sequence[str] | None = None,
        ttl_policy: dict[str, Any] | None = None,
        retry: dict[str, Any] | None = None,
        max_chain_depth: int | None = None,
        logger: Any | None = None,
        verbosity: int = 0,
        on_event: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None: ...

    @property
    def prefixes(self) -> list[str]:
        """The citation prefixes this manager answers to, in registration order."""

    def retrieve(
        self, cites: Iterable[tuple[str, str]]
    ) -> list[CiteFailure]:
        """Fetch every missing or stale citation, following chain pointers.

        Returns the citations that could not be resolved; one bad citation
        never aborts the batch.  Only a cache failure raises.
        """

    def get(self, prefix: str, key: str) -> dict[str, Any]:
        """Read one resolved citation back as a CSL-JSON dict.

        Raises `CitationError` if it is not in the cache.
        """

    def prune(self) -> int:
        """Drop cache entries expired past the grace window; return the count."""
