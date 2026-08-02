#!/usr/bin/env bash
# Builds planner/dist/planner.zip: handler.py + plan.py + their pip
# dependencies, ready for `aws_lambda_function` to pick up. boto3 is
# deliberately NOT bundled -- every Lambda Python runtime provides its own.
set -euo pipefail

cd "$(dirname "$0")"

rm -rf build dist
mkdir -p build dist

cp handler.py plan.py llm.py prompts.py classify.py build/
# Phase 9: the kind-specific half of the Planner is a package, not a flat file.
# It is imported as `import kinds` / `from kinds.value import ...`, so it has to
# land as a directory beside handler.py -- the same flat layout the other
# modules use, one level down.
cp -r kinds build/
# Tests and caches must not travel: they would not break the Lambda, but they
# bloat the zip and put a file named test_* next to production code.
rm -rf build/kinds/__pycache__ build/kinds/test_*.py
# Shared modules, vendored per zip rather than shared as a Layer -- layers are
# still a deferred gap. They are copied flat because that is how they are
# imported at runtime: `import cost`, not `from shared import cost`.
for module in cost.py schedules.py extract.py condition.py fetch.py sources.py; do
  cp "../shared/$module" build/
done
# Wheels are pinned to the Lambda runtime's platform, not the build machine's.
# `pip install -t` on a bare host vendors whatever binaries fit *that* host --
# checker/build/ used to contain _pydantic_core.cpython-312-x86_64-linux-gnu.so
# that worked only because the Codespace happens to match the Lambda runtime.
# Build on a Mac and the zip deploys fine, then dies at import. Known gap since
# Phase 6, deferred then so it would not muddy the browser-Lambda diagnosis.
pip install -r requirements.txt -t build/ --quiet \
  --platform manylinux2014_x86_64 --implementation cp \
  --python-version 3.12 --only-binary=:all:

cd build
zip -r ../dist/planner.zip . --quiet
cd ..

echo "Built dist/planner.zip ($(du -h dist/planner.zip | cut -f1))"
