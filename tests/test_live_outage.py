"""The live check's outage verdict (tests/_live_outage.py), off the network.

Flags come from `verify_flags` over the fake server's real stdio, so the
error class is read from the same wire text the live server sends; the
probe is a stub except in the probe's own test, which drives real sockets
on localhost.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from examples.arf_family.verify_genes import Flag, verify_flags
from tests._live_outage import BACKENDS, OUTAGE, ProbeBroken, outage, probe
from tests.test_verify_genes import FAKE_ARF, _row, _write_genes_tsv

PANTHER_URL = BACKENDS["panther_family"][0][1]


def _flags(tmp_path: Path, rows: list[dict]) -> list[Flag]:
    return asyncio.run(verify_flags(_write_genes_tsv(tmp_path, rows), FAKE_ARF))


def _probe_stub(down: set[str]) -> tuple[list[str], Callable[[str], str | None]]:
    asked: list[str] = []

    def stub(url: str) -> str | None:
        asked.append(url)
        return "ReadTimeout" if url in down else None

    return asked, stub


def test_a_failed_calls_error_class_is_read_from_its_bracket_prefix(tmp_path):
    flags = _flags(
        tmp_path,
        [
            _row("GOOD_ARF_PB1", "ARF5", "PTHR31384:SF10", "true"),
            _row("PANTHER_OUTAGE", "ARF6", "PTHR31384:SF10", "true"),
            _row("CALL_FAILS", "ARF1", "PTHR31384:SF96", "true"),
        ],
    )
    by_locus = {f.locus: f for f in flags}
    assert set(by_locus) == {"PANTHER_OUTAGE", "CALL_FAILS"}  # the good row is clean
    (panther,) = by_locus["PANTHER_OUTAGE"].reasons
    assert (panther.tool, panther.error_class) == ("panther_family", OUTAGE)
    # An error with no bracketed class carries none: never guessed as an outage.
    assert {(r.tool, r.error_class) for r in by_locus["CALL_FAILS"].reasons} == {
        ("interpro_domains", None),
        ("panther_family", None),
    }
    # The joined reason `verify` returns is unchanged.
    assert by_locus["PANTHER_OUTAGE"].reason == (
        "panther_family call failed: [UpstreamUnavailableError] PANTHER geneinfo "
        "exhausted 3 retries (ReadTimeout: )"
    )


def test_an_outage_is_a_skip_only_when_the_direct_probe_finds_the_backend_down(tmp_path):
    flags = _flags(
        tmp_path,
        [
            _row("PANTHER_OUTAGE", "ARF6", "PTHR31384:SF10", "true"),
            _row("NOT_ARF", "ARF1", "PTHR31384:SF96", "true"),  # the planted row
        ],
    )
    asked, down = _probe_stub({PANTHER_URL})
    skip, note = outage(flags, "NOT_ARF", probe=down)
    assert skip == "upstream outage on 1 calls: panther_family (PANTHER geneinfo ReadTimeout)"
    assert note == ""
    assert asked == [PANTHER_URL]  # only the failing tool's backend is asked

    # The same run with PANTHER answering directly is the server's own
    # failure: no skip, and the note says why.
    _, up = _probe_stub(set())
    skip, note = outage(flags, "NOT_ARF", probe=up)
    assert skip is None
    assert "not an outage" in note and "PANTHER geneinfo answer directly" in note


def test_an_outage_beside_any_other_failure_stays_a_failure(tmp_path):
    outage_rows = [
        _row("PANTHER_OUTAGE", "ARF6", "PTHR31384:SF10", "true"),
        _row("NOT_ARF", "ARF1", "PTHR31384:SF96", "true"),
    ]
    _, down = _probe_stub({PANTHER_URL})
    # Positive control: the outage alone is a skip.
    assert outage(_flags(tmp_path, outage_rows), "NOT_ARF", probe=down)[0] is not None

    # A wrong answer on another row in the same run keeps it red.
    wrong = _row("GOOD_ARF_PB1", "ARF5", "PTHR31384:SF99", "true")
    skip, note = outage(_flags(tmp_path, [*outage_rows, wrong]), "NOT_ARF", probe=down)
    assert skip is None
    assert note.startswith("not only an outage: GOOD_ARF_PB1 panther_subfamily mismatch")

    # So does a planted row the discriminator did not catch.
    uncaught = [outage_rows[0], _row("GOOD_ARF_PB1", "ARF5", "PTHR31384:SF10", "true")]
    skip, note = outage(_flags(tmp_path, uncaught), "GOOD_ARF_PB1", probe=down)
    assert skip is None
    assert "the planted row GOOD_ARF_PB1 was not flagged" in note


# InterPro's overloaded database, on a 404 and on a 200 (live, 2026-09-28).
_OVERLOADED = b'{"Error":1040}'
_ANSWERS = {
    "/up": (200, b""),
    "/down": (503, b""),
    "/gone": (404, b""),
    "/overloaded": (404, _OVERLOADED),
    "/overloaded-200": (200, _OVERLOADED),
}


class _Status(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server's name)
        status, body = _ANSWERS[self.path]
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def local_http() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Status)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_probe_reads_up_and_down_and_refuses_anything_else(local_http):
    assert probe(f"{local_http}/up") is None
    assert probe(f"{local_http}/down") == "HTTP 503"
    # Nothing listening: refused on most hosts; some sandboxes drop the
    # connection instead, and it times out. Either is a dead host.
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    refused = probe(f"http://127.0.0.1:{port}/", timeout_s=1)
    assert refused is not None and refused.startswith(("ConnectError", "ConnectTimeout"))
    # Listening but never answering: the timeout PANTHER's outages were.
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        why = probe(f"http://127.0.0.1:{silent.getsockname()[1]}/", timeout_s=0.5)
    assert why is not None and why.startswith("ReadTimeout")
    # A 404 is neither: the probe's own URL is wrong, and it says so.
    with pytest.raises(ProbeBroken, match="HTTP 404"):
        probe(f"{local_http}/gone")
    # Unless the body is the service's own error: InterPro's overloaded
    # database, read as a broken probe on a 404 and as up on a 200.
    assert probe(f"{local_http}/overloaded") == 'HTTP 404 {"Error":1040}'
    assert probe(f"{local_http}/overloaded-200") == 'HTTP 200 {"Error":1040}'
