"""Score one benchmark answer against its question's truth.

Every run ends its reply with ``ANSWER: <value>`` (or ``ANSWER: UNKNOWN``);
the verdict is decided from that line alone, so the same reply always scores
the same way. Verdicts:

- ``right``: the value matches the truth.
- ``wrong``: a value that does not match.
- ``declined``: ``ANSWER: UNKNOWN``.
- ``no_answer``: no ``ANSWER:`` line at all (a format failure, kept apart so it
  is never silently counted as either of the others).

A wrong answer in an identifier category (``symbol_to_locus``,
``uniprot_accession``, ``ortholog``) that is shaped like a real identifier is
also flagged ``wrong_id``: a well-formed id that is not the answer. The flag
checks shape only, not whether the id exists: in the 2026-10-16 run, the one
id it flagged (F4HYE4) is a real but unreviewed UniProt entry for the right
gene, so "wrong id" is all it can say, not "invented".
"""

from __future__ import annotations

import re
from typing import Any

_ANSWER_RE = re.compile(r"^[\s>*_`#-]*answer[*_`]*\s*:\s*(.*?)\s*$", re.IGNORECASE)
_UNKNOWN = {"UNKNOWN", "UNKNOWN.", "N/A", "NONE"}
# Shapes of the ids the identifier questions ask for; a wrong answer with one
# of these shapes is a wrong id rather than a refusal in other words.
_ID_SHAPES = re.compile(
    r"^(AT[1-5CM]G\d{5}|OS\d{2}G\d{7}|LOC_OS\d{2}G\d{5}|ZM\d{5}[A-Z]{1,2}\d{6}"
    r"|SOLYC\d{2}G\d{6}|[OPQ]\d[A-Z0-9]{3}\d|[A-NR-Z]\d([A-Z][A-Z0-9]{2}\d){1,2})$"
)


def extract(reply: str) -> str | None:
    """The value on the reply's last ``ANSWER:`` line, unwrapped; None if none."""
    found = None
    for line in reply.splitlines():
        m = _ANSWER_RE.match(line)
        if m:
            found = m.group(1)
    if found is None:
        return None
    # Some replies wrap the line in <answer>...</answer>; the tags are not the value.
    return re.sub(r"</?answer>", "", found, flags=re.IGNORECASE).strip(" \t*_`")


def _gene_id(value: str) -> str:
    """An id with case, a version suffix and a transcript suffix removed:
    AT1G01010.1, Zm00001eb056300_T001 and Solyc01g005610.3 name their genes."""
    v = value.strip().upper().removeprefix("GENE-")
    v = re.sub(r"_T\d{3}$", "", v)
    v = re.sub(r"\.\d+$", "", v)
    return v


def _chromosome(token: str) -> str:
    t = token.strip().upper()
    t = re.sub(r"^(CHROMOSOME|CHR)\s*", "", t)
    return t.lstrip("0") or "0"


def _location(value: str) -> tuple[str, int] | None:
    """``3:1234567``, ``Chr3:1,234,567`` or ``chromosome 3, 1234567``."""
    m = re.match(r"^\s*(?:chromosome|chr)?\s*([0-9A-Za-z.]+?)\s*[:,]\s*([\d,]+)\b", value, re.I)
    if not m:
        return None
    return _chromosome(m.group(1)), int(m.group(2).replace(",", ""))


def score(question: dict[str, Any], reply: str) -> dict[str, Any]:
    value = extract(reply)
    if value is None:
        return {"verdict": "no_answer", "value": None, "wrong_id": False}
    if value.upper() in _UNKNOWN:
        return {"verdict": "declined", "value": value, "wrong_id": False}
    truth = question["truth"]
    category = question["category"]
    if category == "location":
        got = _location(value)
        names = {_chromosome(c) for c in truth["chromosome"]}
        right = (
            got is not None
            and got[0] in names
            and min(abs(got[1] - truth["start"]), abs(got[1] - truth["end"])) <= 1
        )
    elif category == "protein_length":
        m = re.match(r"^\s*([\d,]+)", value)
        right = m is not None and int(m.group(1).replace(",", "")) == truth["length"]
    elif category == "symbol_to_locus":
        right = _gene_id(value) in {_gene_id(t) for t in truth["loci"]}
    elif category == "uniprot_accession":
        right = re.sub(r"-\d+$", "", value.strip().upper()) == truth["accession"].upper()
    elif category == "ortholog":
        right = _gene_id(value) == _gene_id(truth["gene"])
    else:
        raise ValueError(f"unknown category {category!r}")
    wrong_id = (
        not right
        and category in ("symbol_to_locus", "uniprot_accession", "ortholog")
        and bool(_ID_SHAPES.match(_gene_id(value)))
    )
    return {"verdict": "right" if right else "wrong", "value": value, "wrong_id": wrong_id}
