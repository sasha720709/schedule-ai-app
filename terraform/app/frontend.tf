# Static hosting for the React app: a private S3 bucket, reachable only
# through CloudFront.
#
# The bucket is never public. CloudFront authenticates to it using Origin
# Access Control, which is the current mechanism -- Origin Access Identity is
# the older one still all over the internet, and AWS no longer recommends it.
# OAC signs requests with SigV4, works with SSE-KMS, and is granted by a
# bucket policy that names the distribution rather than an IAM principal.
#
# Like the Fetcher's image, the *content* here lives outside Terraform:
# frontend/deploy.sh builds, syncs and invalidates. Terraform owns the bucket
# and the distribution, not the files in them. Managing hashed build artifacts
# as aws_s3_object resources would mean a plan diff on every rebuild.

resource "aws_s3_bucket" "frontend" {
  # Bucket names are globally unique, so the account id is the disambiguator --
  # same trick the Terraform state bucket uses.
  bucket = "schedule-ai-app-frontend-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "schedule-ai-app-frontend"
  description                       = "Lets the distribution read the private frontend bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Managed policy rather than a hand-rolled one: caches on the URL, respects
# origin cache headers, and compresses. Asset filenames are content-hashed by
# Vite, so only index.html ever needs invalidating.
data "aws_cloudfront_cache_policy" "optimized" {
  name = "Managed-CachingOptimized"
}

resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  default_root_object = "index.html"
  comment             = "schedule-ai-app frontend"
  # US, Canada and Europe. The cheapest class, and there is one user.
  price_class = "PriceClass_100"

  origin {
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = "frontend-bucket"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  default_cache_behavior {
    target_origin_id       = "frontend-bucket"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = data.aws_cloudfront_cache_policy.optimized.id
    compress               = true
  }

  # Single-page-app fallback. A request for a path the bucket has no object
  # for -- which is every client-side route -- would otherwise be a 403 from
  # S3 (403 rather than 404, because the bucket is private and does not admit
  # what does not exist). Rewriting both to index.html with a 200 lets React
  # Router handle the path. Without this, every URL except / is broken.
  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  viewer_certificate {
    # The default *.cloudfront.net certificate. A custom domain would need
    # ACM in us-east-1 plus Route 53, which decision 5 ruled out.
    cloudfront_default_certificate = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }
}

# OAC is granted here, not by an IAM role: the distribution is named as the
# thing allowed to read, via a condition on its ARN. Anything else, including
# a signed URL from a different distribution, is refused.
data "aws_iam_policy_document" "frontend_bucket" {
  statement {
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.frontend.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.frontend.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = data.aws_iam_policy_document.frontend_bucket.json
}
