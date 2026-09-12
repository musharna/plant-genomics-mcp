#!/usr/bin/env bash
# Prove every house-rule hook in .pre-commit-config.yaml can FAIL.
# A guard that has never been seen to reject a violation is not a guard.
# Each case: a planted violation MUST be rejected AND a legitimate line MUST pass.
set -euo pipefail
cd "$(dirname "$0")/.."
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
fail=0
git init -q "$tmp/r"; cp .pre-commit-config.yaml "$tmp/r/"
check() {  # hook, filename, content, expect(pass|fail)
  local hook=$1 f=$2 content=$3 expect=$4 got
  printf '%s\n' "$content" > "$tmp/r/$f"
  ( cd "$tmp/r" && git add -A && uvx pre-commit run "$hook" --files "$f" >/dev/null 2>&1 ) && got=pass || got=fail
  if [[ $got == "$expect" ]]; then echo "ok   $hook [$expect] $f"
  else echo "FAIL $hook expected $expect got $got: $content"; fail=1; fi
  rm -f "$tmp/r/$f"
}
check no-bare-replace a.py 's.replace("a", "b")'            fail
check no-bare-replace b.py 's = s.replace("a", "b")'        pass
check no-bare-replace c.py 'assert s.replace("a","b") == t' pass
check no-dotall-lazy  d.py 're.search(r"<a>(.*?)</a>", x, re.DOTALL)' fail
check no-dotall-lazy  e.py 're.search(r"<a>([^<]*)</a>", x, re.DOTALL)' pass
check no-nohup-background f.sh $'#!/bin/sh\nnohup python worker.py &' fail
check no-nohup-background g.sh $'#!/bin/sh\nsystemd-run --user --unit w python worker.py' pass
check no-uppercase-transform h.css '.legend { text-transform: uppercase; }' fail
check no-uppercase-transform i.css '.legend { font-variant: small-caps; }' pass
check no-dev-paths j.py 'p = "/home/someone/data.csv"' fail
check no-dev-paths k.py 'p = Path(__file__).parent / "data.csv"' pass
exit $fail
