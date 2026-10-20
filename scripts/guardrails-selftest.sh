#!/usr/bin/env bash
# Prove every house-rule hook in .pre-commit-config.yaml can FAIL.
# A guard that has never been seen to reject a violation is not a guard.
# Each case: a planted violation MUST be rejected AND a legitimate line MUST pass.
set -euo pipefail
cd "$(dirname "$0")/.."
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fail=0
git init -q "$tmp/r"
cp .pre-commit-config.yaml "$tmp/r/"
mkdir -p "$tmp/r/scripts"
cp scripts/guardrails-hooks.py "$tmp/r/scripts/"
check() { # hook, filename, content, expect(pass|fail)
	local hook=$1 f=$2 content=$3 expect=$4 got out
	mkdir -p "$(dirname "$tmp/r/$f")"
	printf '%s\n' "$content" >"$tmp/r/$f"
	out=$(cd "$tmp/r" && git add -A && uvx pre-commit run "$hook" --files "$f" 2>&1) && got=pass || got=fail
	# A crash exits 1 too: an AST hook's rejection must name the file and line.
	case $hook in no-bare-replace | no-fixed-wait-py | no-ignored-mutmut-pragma)
		if [[ $got == fail ]] && ! grep -qE "^${f//./\\.}:[0-9]+: " <<<"$out"; then
			got="a crash, not a hit ($(grep -m1 -E 'Error|Exception' <<<"$out" || true))"
		fi
		;;
	esac
	if [[ $got == "$expect" ]]; then
		echo "ok   $hook [$expect] $f"
	else
		echo "FAIL $hook expected $expect got $got: $content"
		fail=1
	fi
	rm -f "$tmp/r/$f"
}
check no-bare-replace a.py 's.replace("a", "b")' fail
check no-bare-replace a2.py $'diag.parse(\n    SAMPLE.replace("x", "y")\n)' pass
check no-bare-replace b.py 's = s.replace("a", "b")' pass
check no-bare-replace c.py 'os.replace(tmp, path)' pass
check no-bare-replace c2.py 'staging.replace(dst)' pass
check no-bare-replace l.py $'import defusedxml.ElementTree as ET\ntry:\n    ET.fromstring(x)\nexcept ET.ParseError:\n    pass' fail
check no-bare-replace l2.py $'from defusedxml import ElementTree\ntry:\n    ElementTree.fromstring(x)\nexcept (ParseError, ValueError):\n    pass' pass
check no-bare-replace l3.py $'import xml.etree.ElementTree as ET\ntry:\n    ET.fromstring(x)\nexcept ET.ParseError:\n    pass' pass
check nosec-needs-reason m.py 'subprocess.call(cmd, shell=True)  # nosec' fail
check nosec-needs-reason m2.py 'subprocess.call(cmd, shell=True)  # nosec B602' fail
check nosec-needs-reason m3.py 'subprocess.call(cmd, shell=True)  # nosec B602 -' fail
check nosec-needs-reason n.py 'subprocess.call(cmd, shell=True)  # nosec B602 - cmd is a module constant' pass
check nosec-needs-reason n2.py 'q = f"SELECT * FROM {t}"  # nosec B608, B610 - t is from a fixed tuple' pass
check bandit-medium-plus sec1.py $'import hashlib\nh = hashlib.md5(b"x").hexdigest()' fail
check bandit-medium-plus sec2.py $'import hashlib\nh = hashlib.md5(b"x", usedforsecurity=False).hexdigest()' pass
check bandit-medium-plus sec3.py $'import hashlib\nh = hashlib.md5(b"x").hexdigest()  # nosec B324 - content fingerprint an external tool also computes' pass
check bandit-medium-plus sec4.py $'import subprocess\nsubprocess.call("ls " + x, shell=True)' fail
check no-raises-bare-exception o.py 'with pytest.raises(Exception):' fail
check no-raises-bare-exception o2.py 'with pytest.raises(Exception, match="boom"):' fail
check no-raises-bare-exception p.py 'with pytest.raises(ValueError, match="boom"):' pass
check no-raises-bare-exception p2.py 'with pytest.raises(ExceptionGroup):' pass
check no-raises-bare-exception p3.py '    # was pytest.raises(Exception, match="x") and it caught a TypeError' pass
check no-raises-bare-exception p4.py '    A bare `pytest.raises(Exception)` passes on almost any breakage.' pass
check no-nested-lazy d.py 're.search(r"(?:<p>.*?</p>)+", x, re.DOTALL)' fail
check no-nested-lazy e.py 're.search(r"<a>(.*?)</a>", x, re.DOTALL)' pass
check no-nohup-background f.sh $'#!/bin/sh\nnohup python worker.py &' fail
check no-nohup-background g.sh $'#!/bin/sh\nsystemd-run --user --unit w python worker.py' pass
check no-uppercase-transform h.css '.legend { text-transform: uppercase; }' fail
check no-uppercase-transform i.css '.legend { font-variant: small-caps; }' pass
check no-uppercase-transform i2.css '/* No text-transform: uppercase - it maps µ to M in unit strings. */' pass
check no-uppercase-transform i3.css $'.legend {\n  text-transform: uppercase;\n}' fail
check no-uppercase-transform i4.css 'h1 { text-transform: uppercase; /* uppercase-ok: page title, no unit text */ }' pass
check no-uppercase-transform i5.css 'h1 { text-transform: uppercase; /* uppercase-ok: */ }' fail
check no-sleep-then-interact s.mjs $'await sleep(240);\nawait page.click("#a");' fail
check no-sleep-then-interact s2.ts $'  await new Promise((r) => setTimeout(r, 500));\n  await page.keyboard.press("Escape");' fail
check no-sleep-then-interact s3.js $'await page.waitForTimeout(300);\nawait page.evaluate(() => document.querySelector("#a").click());' fail
check no-sleep-then-interact s4.mjs $'await sleep(500); // qa-wait-ok:\nawait page.click("#a");' fail
check no-sleep-then-interact t.mjs $'await page.waitForFunction(() => ready());\nawait page.click("#a");' pass
check no-sleep-then-interact t2.mjs $'await sleep(500); // qa-wait-ok: the 420 ms mouse-away close must fire first\nawait page.click("#a");' pass
check no-sleep-then-interact t3.mjs $'await sleep(500);\nconst box = await page.evaluate(() => measure());' pass
check no-sleep-then-interact t4.mjs $'// await sleep(500);\nawait page.click("#a");' pass
check no-fixed-wait-py w.py $'page.wait_for_timeout(800)\nbox = page.evaluate("measure()")' fail
check no-fixed-wait-py w2.py $'async def f(page):\n    await page.wait_for_timeout(300)\n    await page.click("#a")' fail
check no-fixed-wait-py w3.py $'time.sleep(0.5)\npage.keyboard.press("Escape")' fail
check no-fixed-wait-py w4.py $'async def f(page):\n    await asyncio.sleep(1)\n    await page.locator("#a").fill("x")' fail
check no-fixed-wait-py w5.py $'page.wait_for_timeout(500)  # qa-wait-ok:' fail
check no-fixed-wait-py w6.py $'page.wait_for_timeout(\n    150\n)' fail # wrapped by the formatter
check no-fixed-wait-py w7.py $'box = page.wait_for_timeout(5) or page.evaluate("m()")' fail
check no-fixed-wait-py w8.py $'page.wait_for_timeout(5); note = "# qa-wait-ok: a string, not a comment"' fail
check no-fixed-wait-py w9.py $'time.sleep(1)\npage.click(\n    "#a"\n)' fail
check no-fixed-wait-py w10.py $'try:\n    pass\nfinally:\n    time.sleep(1)\n    page.click("#a")' fail
# The sleep pairs with the statement that runs next: out of its block, and into
# what that statement runs first (a class body runs now; a def runs only its
# decorators and defaults now).
check no-fixed-wait-py w11.py $'time.sleep(1)\nclass C:\n    page.click("#a")' fail
check no-fixed-wait-py w12.py $'time.sleep(1)\ncbs = [lambda a=page.click("#a"): a]' fail
check no-fixed-wait-py w13.py $'time.sleep(1)\nif x:\n    @retry(page.click("#a"))\n    def f():\n        pass' fail
check no-fixed-wait-py w14.py $'time.sleep(1)\nif x:\n    def f(*, a=page.click("#a")):\n        pass' fail
check no-fixed-wait-py w15.py $'if x:\n    time.sleep(1)\npage.click("#a")' fail
check no-fixed-wait-py w16.py $'with a:\n    for u in us:\n        time.sleep(1)\npage.click("#a")' fail
check no-fixed-wait-py w17.py $'try:\n    time.sleep(1)\nexcept E:\n    pass\nelse:\n    page.click("#a")' fail
check no-fixed-wait-py w18.py $'try:\n    f()\nexcept E:\n    time.sleep(1)\nfinally:\n    page.click("#a")' fail
check no-fixed-wait-py w19.py $'for u in us:\n    time.sleep(1)\nelse:\n    page.click("#a")' fail
check no-fixed-wait-py w20.py $'try:\n    f()\nexcept E:\n    pass\nelse:\n    time.sleep(1)\nfinally:\n    page.click("#a")' fail
check no-fixed-wait-py w21.py $'try:\n    time.sleep(1)\nfinally:\n    page.click("#a")' fail
check no-fixed-wait-py w22.py $'time.sleep(1)\nrecord(page.click("#a"), 2)' fail
# A sleep ending a loop body also runs before the loop's next pass, at every depth.
check no-fixed-wait-py w23.py $'for u in us:\n    page.click("#a")\n    time.sleep(1)' fail
check no-fixed-wait-py w24.py $'while True:\n    page.keyboard.press("PageDown")\n    time.sleep(0.5)' fail
check no-fixed-wait-py w25.py $'for a in xs:\n    page.click(a)\n    for b in ys:\n        f(b)\n        time.sleep(1)' fail
# An empty iterable runs a for loop's else first.
check no-fixed-wait-py w26.py $'time.sleep(1)\nfor u in us:\n    log(u)\nelse:\n    page.click("#a")' fail
check no-fixed-wait-py y.py $'time.sleep(1)\ndef helper(page):\n    page.click("#a")' pass
check no-fixed-wait-py y2.py $'time.sleep(1)\ncb = lambda: page.click("#a")' pass
check no-fixed-wait-py y3.py $'if x:\n    time.sleep(1)\nelse:\n    page.click("#a")' pass
check no-fixed-wait-py y4.py $'try:\n    f()\nexcept A:\n    time.sleep(1)\nexcept B:\n    page.click("#a")' pass
check no-fixed-wait-py y5.py $'def f():\n    time.sleep(1)\npage.click("#a")' pass
check no-fixed-wait-py y6.py $'time.sleep(1)\nfor u in urls:\n    page.goto(u)\n    page.click("#a")' pass
check no-fixed-wait-py y7.py $'time.sleep(1)\ntry:\n    f()\nexcept E:\n    page.click("#a")\nfinally:\n    page.click("#b")' pass
check no-fixed-wait-py y8.py $'time.sleep(1)\npage.mouse.wheel(0, 120)' pass
check no-fixed-wait-py y9.py $'for u in us:\n    page.goto(u)\n    page.click("#a")\n    time.sleep(1)' pass
check no-fixed-wait-py y10.py $'for u in us:\n    page.click("#a")\n    time.sleep(1)  # qa-wait-ok: the API allows one request a second' pass
check no-fixed-wait-py x.py $'page.wait_for_function("ready()")\npage.click("#a")' pass
check no-fixed-wait-py x2.py $'page.wait_for_timeout(500)  # qa-wait-ok: the 420 ms close animation is what is tested' pass
check no-fixed-wait-py x3.py $'time.sleep(2 ** attempt)\nresp = client.get(url)' pass
check no-fixed-wait-py x4.py $'# page.wait_for_timeout(800)\npage.click("#a")' pass
check no-fixed-wait-py x5.py $'time.sleep(0.5)  # qa-wait-ok: the debounce window is the behaviour tested\npage.click("#a")' pass
check no-fixed-wait-py x6.py $'page.wait_for_timeout(\n    150\n)  # qa-wait-ok: paces wheel notches; the state wait follows' pass
check no-ignored-mutmut-pragma p.py $'x = f(\n    a,  # pragma: no mutate\n)' fail
check no-ignored-mutmut-pragma p2.py $'x = f(\n    a,\n)  # pragma: no mutate' fail
check no-ignored-mutmut-pragma p3.py $'m = (\n    a\n    or b  # pragma: no mutate: "" reads alike\n)' fail
check no-ignored-mutmut-pragma q.py 'x = f(a)  # pragma: no mutate' pass
check no-ignored-mutmut-pragma q2.py $'for a, b in zip(x, y, strict=True):  # pragma: no mutate\n    pass' pass
check no-ignored-mutmut-pragma q3.py $'# pragma: no mutate start\nx = f(\n    a,\n)\n# pragma: no mutate end' pass
check no-ignored-mutmut-pragma q4.py 'note = "a,  # pragma: no mutate"' pass
# Every hit is reported, not only the first (pygrep --multiline stops at one per file).
printf '%s\n' 'page.wait_for_timeout(1)' 'x = 1' 'page.wait_for_timeout(2)' 'page.wait_for_timeout(3)' >"$tmp/r/m.py"
hits=$(cd "$tmp/r" && git add -A && uvx pre-commit run no-fixed-wait-py --files m.py 2>&1 | grep -cE '^m\.py:[0-9]+:' || true)
if [[ $hits == 3 ]]; then echo "ok   no-fixed-wait-py [3 of 3 reported] m.py"; else echo "FAIL no-fixed-wait-py reported $hits of 3 hits in m.py"; fail=1; fi
rm -f "$tmp/r/m.py"
wf=.github/workflows
check no-pathscoped-write-rule $wf/w1.yml '            --allowed-tools "Read,Write(fuzz_scratch/**),Grep"' fail
check no-pathscoped-write-rule $wf/w2.yml '            --allowed-tools "Bash(gh issue view:*),Edit(/tmp/*),Glob"' fail
check no-pathscoped-write-rule templates/workflows/w3.yml '  --allowedTools "Read(/etc/*)"' fail
check no-pathscoped-write-rule $wf/w4.yml '            --allowed-tools Read,Write(fuzz_scratch/**),Grep' fail
check no-pathscoped-write-rule $wf/w5.yml '            --allowedTools=Edit(/tmp/*)' fail
check no-pathscoped-write-rule $wf/w6.yml '            --allowed-tools Read, Write(fuzz_scratch/**), Grep' fail
check no-pathscoped-write-rule $wf/x5.yml '            --allowed-tools Read,Edit(fuzz_scratch/**),Grep # not Write(x)' pass
check no-pathscoped-write-rule $wf/x1.yml '            --allowed-tools "Read,Edit(fuzz_scratch/**),Grep"' pass
check no-pathscoped-write-rule $wf/x2.yml '            --allowed-tools "Bash(gh issue comment:*),Edit(//tmp/**),Glob"' pass
check no-pathscoped-write-rule $wf/x3.yml '            --allowed-tools "Read,Write,Grep"' pass
check no-pathscoped-write-rule $wf/x4.yml '            --allowed-tools "Read(//tmp/**),Grep"' pass
devroot="/mnt/c/Us" # joined at runtime so this file never contains the literal path it plants
devfix=$(printf 'p = "%sers/a2b32/Zotero/x.pdf"' "$devroot")
check no-dev-paths j.py "$devfix" fail
homefix=$(printf 'p = "/home%s"' "/someone/data.csv") # placeholder home must PASS
check no-dev-paths k.py "$homefix" pass
mkdir -p "$tmp/r/.github/workflows"
check no-unpinned-action .github/workflows/u1.yml '      - uses: actions/checkout@v7' fail
check no-unpinned-action .github/workflows/u2.yml '        uses: actions/setup-node@v7.1.0 # v7.1.0' fail
check no-unpinned-action .github/workflows/u3.yml '      - uses: actions/checkout@3d3c42e # short SHA' fail
check no-unpinned-action .github/workflows/u4.yaml '    uses: "owner/repo/.github/workflows/build.yml@main"' fail
check no-unpinned-action .github/workflows/v1.yml '      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1' pass
check no-unpinned-action .github/workflows/v2.yml '      - uses: ./.github/actions/setup' pass
check no-unpinned-action .github/workflows/v3.yml '      # was uses: actions/checkout@v7' pass
check no-unpinned-action u5.md '      - uses: actions/checkout@v7' pass # outside workflows: prose may quote a tag
check no-unpinned-action .github/workflows/u6.yml '      - uses: docker://alpine:3.20' fail
check no-unpinned-action .github/workflows/u7.yml '      - uses: docker://ghcr.io/o/img@sha256:abc123' fail
check no-unpinned-action .github/workflows/v4.yml '      - uses: docker://alpine@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' pass
mkdir -p "$tmp/r/.github/actions/setup"
check no-unpinned-action .github/actions/setup/action.yml '      - uses: actions/setup-node@v7' fail # composite action
check no-unpinned-action action.yaml '    - uses: actions/checkout@v7' fail # root composite action
check no-unpinned-action .github/actions/setup/action.yml '      - uses: actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0' pass
exit $fail
