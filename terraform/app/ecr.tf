# Container-image Lambdas pull from ECR rather than reading a zip, so the
# registry has to exist before the image can be pushed, and the image has
# to exist before the Lambda can be created. Hence ECR lives here on its
# own: terraform apply -> build.sh push -> terraform apply again.
resource "aws_ecr_repository" "fetcher" {
  name                 = "schedule-ai-app-fetcher"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Untagged images pile up on every rebuild and ECR storage is billed by
# the GB -- with a ~2GB image that adds up fast.
resource "aws_ecr_lifecycle_policy" "fetcher" {
  repository = aws_ecr_repository.fetcher.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after 1 day"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 1
      }
      action = { type = "expire" }
    }]
  })
}
