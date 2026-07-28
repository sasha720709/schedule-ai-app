#!/usr/bin/env bash
# Builds planner/dist/planner.zip: handler.py + plan.py + their pip
# dependencies, ready for `aws_lambda_function` to pick up. boto3 is
# deliberately NOT bundled -- every Lambda Python runtime provides its own.
set -euo pipefail

cd "$(dirname "$0")"

rm -rf build dist
mkdir -p build dist

cp handler.py plan.py build/
pip install -r requirements.txt -t build/ --quiet

cd build
zip -r ../dist/planner.zip . --quiet
cd ..

echo "Built dist/planner.zip ($(du -h dist/planner.zip | cut -f1))"
