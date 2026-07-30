#!/usr/bin/env bash
# Builds checker/dist/checker.zip. Same shape as planner/build.sh.
set -euo pipefail

cd "$(dirname "$0")"

rm -rf build dist
mkdir -p build dist

cp handler.py check.py build/
# The cost model is shared by three Lambdas. Vendored per zip rather than
# shared as a Layer, which is still a deferred gap.
cp ../shared/cost.py build/
pip install -r requirements.txt -t build/ --quiet

cd build
zip -r ../dist/checker.zip . --quiet
cd ..

echo "Built dist/checker.zip ($(du -h dist/checker.zip | cut -f1))"
