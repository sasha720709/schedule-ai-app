#!/usr/bin/env bash
# Builds api/dist/api.zip. No pip install step -- this Lambda only uses
# boto3, which the runtime ships.
set -euo pipefail

cd "$(dirname "$0")"

rm -rf build dist
mkdir -p build dist

cp handler.py build/

cd build
zip -r ../dist/api.zip . --quiet
cd ..

echo "Built dist/api.zip ($(du -h dist/api.zip | cut -f1))"
