#!/usr/bin/env bash
# Builds the browser-fetcher container image and pushes it to ECR.
# Unlike the other Lambdas' build.sh (which just zip), this one needs
# Docker and an ECR login -- container images live in a registry, not
# in a file Terraform can upload.
set -euo pipefail

cd "$(dirname "$0")"

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REPO="schedule-ai-app-fetcher"
URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${REPO}"

echo "==> logging Docker in to ECR"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

echo "==> building (this pulls ~2GB of Playwright base image the first time)"
docker build --platform linux/amd64 -t "${REPO}:latest" .

echo "==> pushing to ${URI}:latest"
docker tag "${REPO}:latest" "${URI}:latest"
docker push "${URI}:latest"

echo
echo "Pushed ${URI}:latest"
echo "Now run terraform apply to point the Lambda at it."
