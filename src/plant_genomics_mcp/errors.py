"""Exception hierarchy for plant-genomics-mcp.

All errors inherit from ``PlantGenomicsError`` so callers can keep a single
``except`` catch-all. Subclasses let an LLM client route on the kind of
failure without parsing the message.

The base ``__str__`` prepends the subclass name so the MCP SDK's default
error-result serializer (which stringifies the exception) preserves the
type information on the wire. Example wire payload:

    [RateLimitError] Ensembl Plants /lookup/id/AT1G01010 exhausted 3 retries

This shape lets an upstream LLM regex on the bracket prefix to differentiate
a 429-backoff situation from a not-found situation.
"""

from __future__ import annotations


class PlantGenomicsError(RuntimeError):
    """Base error raised by any plant-genomics-mcp backend.

    Subclasses below carry the same message contract but encode the
    *kind* of failure in the type. Catch this base class to catch all
    plant-genomics-mcp errors at once.
    """

    def __str__(self) -> str:
        msg = super().__str__()
        # Prepend the leaf class name so SDK str-serialization preserves
        # the type. Don't prepend on the base class itself — it'd just
        # add noise.
        if type(self) is PlantGenomicsError:
            return msg
        return f"[{type(self).__name__}] {msg}"


class RateLimitError(PlantGenomicsError):
    """Raised when a backend exhausts its 429 retry budget.

    LLM clients should treat this as transient and back off before
    retrying — not as a missing record.
    """


class NotFoundError(PlantGenomicsError):
    """Raised when a locus / record does not exist upstream.

    Covers 404 responses, empty-result BioMart bodies, and invalid
    locus identifiers (where the input could not possibly resolve).
    LLM clients should treat this as terminal for the given input.
    """


class UpstreamUnavailableError(PlantGenomicsError):
    """Raised when a backend is unreachable or 5xx-erroring past its retries.

    LLM clients should treat this as a service outage and consider
    falling back to a peer backend (e.g. Phytozome when Ensembl is down).
    """


class OrganismNotFound(PlantGenomicsError):
    """Input did not match any record in the organisms registry.

    Carries the supported organism list so the caller (typically an LLM)
    can pick a valid one without a second round trip.
    """

    def __init__(self, query: str | int, *, supported: list[str]) -> None:
        super().__init__(f"organism {query!r} not in registry; supported: {sorted(supported)}")
        self.query = query
        self.supported = sorted(supported)


class OrganismNotSupported(PlantGenomicsError):
    """Input resolved, but the requested backend has no ID for it.

    Synthesis tools catch this and translate it into ``StepRow(status="skipped")``
    so partial-success semantics carry through the envelope.
    """

    def __init__(
        self,
        *,
        backend: str,
        organism: str,
        supported: list[str],
    ) -> None:
        super().__init__(
            f"backend {backend!r} has no ID for {organism!r}; "
            f"supported by {backend!r}: {sorted(supported)}"
        )
        self.backend = backend
        self.organism = organism
        self.supported = sorted(supported)
