"""Extract the failing locus_ids from a benchmark sidecar (last_run JSON).

Used by the scheduled-monitoring workflow's two-strikes re-run: after a run
exits non-zero, this lists exactly which loci carried an exit-triggering verdict
so the workflow can re-run ONLY those (`--loci`), and page only if the same loci
fail twice.

"Failing" is defined by ``benchmark_annotations.EXIT_TRIGGERING_VERDICTS`` — the
same set ``BenchmarkSummary.exit_code`` uses — imported here so the two cannot
drift. PASS / DRIFT / EXCEPTION_OK / SKIPPED are NOT failing.

Usage:
  python scripts/benchmark_failing_loci.py <sidecar.json>
    -> prints failing locus_ids comma-separated on one line (empty if none).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark_annotations import EXIT_TRIGGERING_VERDICTS  # noqa: E402

_FAILING_VALUES = {v.value for v in EXIT_TRIGGERING_VERDICTS}


def _has_failing_verdict(node: Any) -> bool:  # noqa: ANN401
    """True if any ``"verdict"`` anywhere under ``node`` is exit-triggering.

    Recurses dicts/lists so it does not hard-code the sidecar nesting
    (``tools[].assertions[].verdict``, ``invariants[].verdict``,
    ``probe_exceptions``) — robust to future shape changes.
    """
    if isinstance(node, dict):
        v = node.get("verdict")
        if isinstance(v, str) and v in _FAILING_VALUES:
            return True
        return any(_has_failing_verdict(child) for child in node.values())
    if isinstance(node, list):
        return any(_has_failing_verdict(child) for child in node)
    return False


def failing_loci(sidecar: dict[str, Any]) -> list[str]:
    """locus_ids with >=1 exit-triggering verdict, in sidecar (corpus) order."""
    out: list[str] = []
    for locus in sidecar.get("loci", []):
        locus_id = locus.get("locus_id")
        if locus_id is None:
            continue
        if _has_failing_verdict(locus):
            out.append(locus_id)
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: benchmark_failing_loci.py <sidecar.json>", file=sys.stderr)
        return 2
    sidecar = json.loads(Path(args[0]).read_text())
    print(",".join(failing_loci(sidecar)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
