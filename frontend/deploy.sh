#!/usr/bin/env bash
# Builds the React app and publishes it.
#
# The same division as fetcher/build.sh: Terraform owns the bucket and the
# distribution, this owns what goes in them. Managing content-hashed build
# artifacts as Terraform resources would produce a plan diff on every rebuild.
set -euo pipefail

cd "$(dirname "$0")"

BUCKET="$(cd ../terraform/app && terraform output -raw frontend_bucket)"
DIST="$(cd ../terraform/app && terraform output -raw frontend_distribution_id)"
URL="$(cd ../terraform/app && terraform output -raw frontend_url)"

echo "==> building"
npm run build

# Hashed asset filenames can be cached hard and forever; index.html cannot,
# because its name never changes and it is what points at the new hashes.
echo "==> syncing assets (immutable)"
aws s3 sync dist/ "s3://${BUCKET}/" \
  --delete \
  --exclude index.html \
  --cache-control "public,max-age=31536000,immutable"

echo "==> syncing index.html (no-cache)"
aws s3 cp dist/index.html "s3://${BUCKET}/index.html" \
  --cache-control "no-cache"

# Only index.html needs invalidating -- everything else is a new filename, so
# CloudFront has never seen it and has nothing stale to forget. Invalidating
# /* on every deploy is the common reflex and it is wasteful: the first 1000
# paths a month are free, then they are billed per path.
echo "==> invalidating /index.html"
aws cloudfront create-invalidation \
  --distribution-id "$DIST" \
  --paths /index.html \
  --query 'Invalidation.Id' --output text

echo
echo "Deployed to ${URL}"
