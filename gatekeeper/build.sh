#!/usr/bin/env bash
# Builds gatekeeper/dist/gatekeeper.zip. No dependencies at all -- this Lambda
# reads one environment variable and compares strings.
set -euo pipefail

cd "$(dirname "$0")"

rm -rf build dist
mkdir -p build dist

cp handler.py build/

cd build
zip -r ../dist/gatekeeper.zip . --quiet
cd ..

echo "Built dist/gatekeeper.zip ($(du -h dist/gatekeeper.zip | cut -f1))"
