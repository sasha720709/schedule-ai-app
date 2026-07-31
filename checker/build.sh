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
for module in cost.py extract.py condition.py fetch.py; do
  cp "../shared/$module" build/
done
pip install -r requirements.txt -t build/ --quiet

cd build
zip -r ../dist/checker.zip . --quiet
cd ..

echo "Built dist/checker.zip ($(du -h dist/checker.zip | cut -f1))"
