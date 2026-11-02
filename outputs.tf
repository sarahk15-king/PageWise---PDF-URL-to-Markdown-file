output "api_url" {
  description = "Where customers send requests."
  value       = "${var.certificate_arn == "" ? "http" : "https"}://${aws_lb.api.dns_name}"
}

output "ecr_repository_url" {
  description = "Push the container image here."
  value       = aws_ecr_repository.app.repository_url
}

output "bucket_name" {
  value = aws_s3_bucket.data.bucket
}

output "jobs_queue_url" {
  value = aws_sqs_queue.jobs.id
}

output "tables" {
  value = {
    jobs     = aws_dynamodb_table.jobs.name
    api_keys = aws_dynamodb_table.api_keys.name
    usage    = aws_dynamodb_table.usage.name
  }
}

output "secrets_to_fill" {
  description = "Put the real values into these two secrets after the first apply."
  value       = [aws_secretsmanager_secret.azure.name, aws_secretsmanager_secret.app.name]
}