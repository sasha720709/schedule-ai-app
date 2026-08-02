#!/usr/bin/env bash
# Builds checker/dist/checker.zip. Same shape as planner/build.sh.
set -euo pipefail

cd "$(dirname "$0")"

rm -rf build dist
mkdir -p build dist

cp handler.py check.py build/
# Shared modules, vendored per zip rather than shared as a Layer -- layers are
# still a deferred gap. They are copied flat because that is how they are
# imported at runtime: `import cost`, not `from shared import cost`.
for module in cost.py extract.py condition.py fetch.py repair.py; do
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
zip -r ../dist/checker.zip . --quiet
cd ..

echo "Built dist/checker.zip ($(du -h dist/checker.zip | cut -f1))"
