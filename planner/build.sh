#!/usr/bin/env bash
# Builds planner/dist/planner.zip: handler.py + plan.py + their pip
# dependencies, ready for `aws_lambda_function` to pick up. boto3 is
# deliberately NOT bundled -- every Lambda Python runtime provides its own.
set -euo pipefail

cd "$(dirname "$0")"

rm -rf build dist
mkdir -p build dist

cp handler.py plan.py build/
# Shared modules, vendored per zip rather than shared as a Layer -- layers are
# still a deferred gap. They are copied flat because that is how they are
# imported at runtime: `import cost`, not `from shared import cost`.
for module in cost.py extract.py condition.py fetch.py sources.py; do
  cp "../shared/$module" build/
done
pip install -r requirements.txt -t build/ --quiet

cd build
zip -r ../dist/planner.zip . --quiet
cd ..

echo "Built dist/planner.zip ($(du -h dist/planner.zip | cut -f1))"
