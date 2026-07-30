#!/usr/bin/env bash
# Builds authorizer/dist/authorizer.zip. No pip install step -- this Lambda
# only uses boto3, which the runtime ships.
set -euo pipefail

cd "$(dirname "$0")"

rm -rf build dist
mkdir -p build dist

cp handler.py build/

cd build
zip -r ../dist/authorizer.zip . --quiet
cd ..

echo "Built dist/authorizer.zip ($(du -h dist/authorizer.zip | cut -f1))"
