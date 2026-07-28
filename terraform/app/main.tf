# Application infrastructure: DynamoDB tables, Lambdas, EventBridge Scheduler.
# Unlike terraform/bootstrap, this module stores its state remotely, in the
# bucket + lock table that bootstrap created.

terraform {
  required_version = ">= 1.7.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket         = "schedule-ai-app-tfstate-851725214678"
    key            = "app/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "schedule-ai-app-tfstate-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}
