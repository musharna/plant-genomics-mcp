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
	local hook=$1 f=$2 content=$3 expect=$4 got
	printf '%s\n' "$content" >"$tmp/r/$f"
	(cd "$tmp/r" && git add -A && uvx pre-commit run "$hook" --files "$f" >/dev/null 2>&1) && got=pass || got=fail
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
check no-raises-bare-exception o.py 'with pytest.raises(Exception):' fail
check no-raises-bare-exception o2.py 'with pytest.raises(Exception, match="boom"):' fail
check no-raises-bare-exception p.py 'with pytest.raises(ValueError, match="boom"):' pass
check no-raises-bare-exception p2.py 'with pytest.raises(ExceptionGroup):' pass
check no-nested-lazy d.py 're.search(r"(?:<p>.*?</p>)+", x, re.DOTALL)' fail
check no-nested-lazy e.py 're.search(r"<a>(.*?)</a>", x, re.DOTALL)' pass
check no-nohup-background f.sh $'#!/bin/sh\nnohup python worker.py &' fail
check no-nohup-background g.sh $'#!/bin/sh\nsystemd-run --user --unit w python worker.py' pass
check no-uppercase-transform h.css '.legend { text-transform: uppercase; }' fail
check no-uppercase-transform i.css '.legend { font-variant: small-caps; }' pass
devroot="/mnt/c/Us" # joined at runtime so this file never contains the literal path it plants
devfix=$(printf 'p = "%sers/a2b32/Zotero/x.pdf"' "$devroot")
check no-dev-paths j.py "$devfix" fail
homefix=$(printf 'p = "/home%s"' "/someone/data.csv") # placeholder home must PASS
check no-dev-paths k.py "$homefix" pass
exit $fail
