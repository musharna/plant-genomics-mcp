"""BioCyc XML is remote input and must go through defusedxml.

Negative control: an entity-expansion payload is rejected. Positive control in the
same test: a plain document still parses, so a broken parser cannot read as "blocked".
"""

import pytest

from plant_genomics_mcp import plantcyc
from plant_genomics_mcp.errors import PlantGenomicsError

BOMB = (
    '<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "xxxxxxxxxx">'
    '<!ENTITY y "&x;&x;&x;&x;&x;&x;&x;&x;&x;&x;">]><a>&y;</a>'
)


def test_parse_rejects_entities_and_parses_plain():
    assert plantcyc._parse("<a><b>1</b></a>", "t").find("b").text == "1"
    # PlantGenomicsError, not bare Exception: the rejection must come out WRAPPED
    # with the source label, or the server reports a bare defusedxml message
    with pytest.raises(
        PlantGenomicsError, match=r"PlantCyc t returned unparseable XML: .*(?i:entit)"
    ):
        plantcyc._parse(BOMB, "t")
