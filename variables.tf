variable "aws_region" {
  description = "Region for everything. Bedrock must offer the Nova model in this region."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Prefix for every resource name."
  type        = string
  default     = "doc-extract"
}

variable "environment" {
  description = "dev, staging or prod. Part of every resource name."
  type        = string
  default     = "dev"
}

variable "deploy_services" {
  description = "false creates everything except the running containers, so the image can be pushed first. Set true after the first push."
  type        = bool
  default     = false
}

variable "image_tag" {
  description = "Tag of the container image in ECR."
  type        = string
  default     = "latest"
}

variable "api_command" {
  description = "Command that starts the API container."
  type        = list(string)
  default     = ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
}

variable "worker_command" {
  description = "Command that starts the worker container. The worker is built in Task 5."
  type        = list(string)
  default     = ["python", "-m", "engine.worker"]
}

variable "api_port" {
  type    = number
  default = 8000
}

variable "api_cpu" {
  description = "Fargate CPU units for the API."
  type        = number
  default     = 512
}

variable "api_memory" {
  description = "Fargate memory in MiB for the API."
  type        = number
  default     = 1024
}

variable "worker_cpu" {
  description = "Fargate CPU units for each worker."
  type        = number
  default     = 1024
}

variable "worker_memory" {
  description = "Fargate memory in MiB for each worker. PDF rendering needs more than the API."
  type        = number
  default     = 4096
}

variable "worker_min" {
  description = "Fewest workers kept running."
  type        = number
  default     = 1
}

variable "worker_max" {
  description = "Most workers started when the queue grows."
  type        = number
  default     = 5
}

variable "queue_visibility_seconds" {
  description = "How long a job stays hidden after a worker takes it. Longer than the slowest job."
  type        = number
  default     = 900
}

variable "input_retention_days" {
  description = "Uploaded PDFs are deleted after this many days."
  type        = number
  default     = 30
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "bedrock_model_id" {
  description = "Amazon Nova model used for AI repair."
  type        = string
  default     = "amazon.nova-lite-v1:0"
}

variable "ai_repair_enabled" {
  description = "AI repair is off by default. Turn it on once Bedrock access is confirmed."
  type        = bool
  default     = false
}

variable "certificate_arn" {
  description = "ACM certificate for HTTPS. Leave empty for HTTP only."
  type        = string
  default     = ""
}

variable "force_destroy" {
  description = "true lets terraform destroy empty the bucket and ECR repository. Set false for production."
  type        = bool
  default     = true
}