#!/usr/bin/env bash
# Builds notifier/dist/notifier.zip. No pip install step -- this Lambda
# needs nothing beyond boto3, which the runtime ships.
set -euo pipefail

cd "$(dirname "$0")"

rm -rf build dist
mkdir -p build dist

cp handler.py build/

cd build
zip -r ../dist/notifier.zip . --quiet
cd ..

echo "Built dist/notifier.zip ($(du -h dist/notifier.zip | cut -f1))"
