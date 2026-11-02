locals {
  name = "${var.project}-${var.environment}"
}

# ---- S3: uploaded PDFs and extraction results -------------------------------------------------

resource "aws_s3_bucket" "data" {
  bucket        = "${local.name}-${data.aws_caller_identity.current.account_id}"
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    id     = "expire-uploads"
    status = "Enabled"

    filter {
      prefix = "uploads/"
    }

    expiration {
      days = var.input_retention_days
    }
  }
}

# ---- SQS: the queue of extraction jobs, and a dead-letter queue for jobs that keep failing -----

resource "aws_sqs_queue" "dead" {
  name                      = "${local.name}-jobs-dead"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "jobs" {
  name                       = "${local.name}-jobs"
  visibility_timeout_seconds = var.queue_visibility_seconds
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dead.arn
    maxReceiveCount     = 3
  })
}

# ---- DynamoDB: job status, API keys and usage ------------------------------------------------

resource "aws_dynamodb_table" "jobs" {
  name         = "${local.name}-jobs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  attribute {
    name = "key_id"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  global_secondary_index {
    name            = "by_key"
    hash_key        = "key_id"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }
}

resource "aws_dynamodb_table" "api_keys" {
  name         = "${local.name}-api-keys"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "key_hash"

  attribute {
    name = "key_hash"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }
}

resource "aws_dynamodb_table" "usage" {
  name         = "${local.name}-usage"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "key_id"
  range_key    = "period"

  attribute {
    name = "key_id"
    type = "S"
  }

  attribute {
    name = "period"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }
}

# ---- Secrets Manager: keys the containers read at start-up ------------------------------------
# Terraform creates the secret with empty placeholders. Put the real values in with the console or the CLI,
# so they never appear in Terraform files or state.

resource "aws_secretsmanager_secret" "azure" {
  name                    = "${local.name}/azure-docintel"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "azure" {
  secret_id = aws_secretsmanager_secret.azure.id
  secret_string = jsonencode({
    AZURE_DOCINTEL_ENDPOINT = ""
    AZURE_DOCINTEL_KEY      = ""
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "app" {
  name                    = "${local.name}/app"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    API_KEY_PEPPER = "change-me-after-first-apply"
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}