"""Render the README cards (*.html here) to 1600x900 PNGs in examples/assets.

Each card is a still of a real Claude Code answer (see RUN.md); the font size
is fitted so the text fills the box without overflowing, then written back to
the HTML so the source matches the image. Needs Playwright + Chromium:
    uv run --with playwright python examples/assets/cards/render.py
"""

import re
import signal
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

signal.signal(signal.SIGALRM, lambda *_: sys.exit("aborting: walltime guard"))
signal.alarm(120)
HERE = Path(__file__).resolve().parent
CARDS = {"answer.html": "cc-answer.png", "literature.html": "cc-literature.png"}


# The fonts each part must be drawn in. The cards name installed fonts, not
# @font-face ones, so document.fonts never sees them: a missing font is a silent
# fallback (DejaVu Sans Mono, Liberation Sans), and FITS would fit its metrics.
# Chromium reports the font it actually drew each node with; that is checked.
FONTS = {"pre": "JetBrainsMono", ".cap": "Lato"}


def settle(page, src: Path) -> None:
    """Load ``src`` and refuse it unless every part is drawn in its own font."""
    page.goto(src.as_uri(), wait_until="load")
    cdp = page.context.new_cdp_session(page)
    cdp.send("DOM.enable")
    cdp.send("CSS.enable")
    root = cdp.send("DOM.getDocument")["root"]["nodeId"]
    for selector, family in FONTS.items():
        node = cdp.send("DOM.querySelector", {"nodeId": root, "selector": selector})["nodeId"]
        used = cdp.send("CSS.getPlatformFontsForNode", {"nodeId": node})["fonts"]
        drawn = max(used, key=lambda f: f["glyphCount"])["familyName"]
        if not drawn.startswith(family):
            sys.exit(f"{src.name}: {selector} drawn in {drawn!r}, not {family} (install it)")
    cdp.detach()


FITS = """(()=>{const e=document.querySelector('pre'), w=document.querySelector('.wrap');
  return e.scrollWidth<=e.clientWidth && w.scrollHeight<=w.clientHeight})()"""

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--disable-gpu", "--use-angle=swiftshader"])
    page = browser.new_page(viewport={"width": 1600, "height": 900})
    for html, png in CARDS.items():
        src = HERE / html
        settle(page, src)
        best = None
        for px in range(60, 20, -1):
            page.evaluate(f"document.querySelector('pre').style.fontSize='{px}px'")
            if page.evaluate(FITS):
                best = px
                break
        if best is None:
            sys.exit(f"{html}: text does not fit even at 21px")
        text, n = re.subn(r"font:\d+px/1\.5 ", f"font:{best}px/1.5 ", src.read_text())
        assert n == 1, html
        src.write_text(text)
        settle(page, src)
        assert page.evaluate(FITS), html
        page.screenshot(path=str(HERE.parent / png))
        print(f"{png}: {best}px")
    browser.close()
