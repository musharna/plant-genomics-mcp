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
FITS = """(()=>{const e=document.querySelector('pre'), w=document.querySelector('.wrap');
  return e.scrollWidth<=e.clientWidth && w.scrollHeight<=w.clientHeight})()"""

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--disable-gpu", "--use-angle=swiftshader"])
    page = browser.new_page(viewport={"width": 1600, "height": 900})
    for html, png in CARDS.items():
        src = HERE / html
        page.goto(src.as_uri())
        page.wait_for_timeout(800)
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
        page.goto(src.as_uri())
        page.wait_for_timeout(800)
        assert page.evaluate(FITS), html
        page.screenshot(path=str(HERE.parent / png))
        print(f"{png}: {best}px")
    browser.close()
