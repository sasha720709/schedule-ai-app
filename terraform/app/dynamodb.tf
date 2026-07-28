# Watches: one row per watch a user creates.
resource "aws_dynamodb_table" "watches" {
  name         = "schedule-ai-app-watches"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "watch_id"

  attribute {
    name = "watch_id"
    type = "S"
  }
}

# WatchTargets: one row per URL the Planner decided to monitor for a watch.
# A watch can have several targets, so target_id is the key and watch_id
# gets a GSI so the Checker can list "all targets for this watch."
resource "aws_dynamodb_table" "watch_targets" {
  name         = "schedule-ai-app-watch-targets"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "target_id"

  attribute {
    name = "target_id"
    type = "S"
  }

  attribute {
    name = "watch_id"
    type = "S"
  }

  global_secondary_index {
    name            = "watch_id-index"
    hash_key        = "watch_id"
    projection_type = "ALL"
  }
}
