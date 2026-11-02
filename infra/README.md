# Deploying the service with Terraform

This folder creates everything the service runs on in AWS. One command builds it and one command removes it.

| Part | Resource |
|---|---|
| Public entrance | Application Load Balancer, then the API on ECS Fargate |
| Files | S3 bucket: public access blocked, encrypted, uploads deleted after 30 days |
| Jobs | SQS queue with a dead-letter queue for jobs that fail three times |
| Workers | ECS Fargate service, scaled up and down by the length of the queue |
| Job status, API keys, usage and cost | Three DynamoDB tables |
| Secrets | Two Secrets Manager secrets (Azure keys and the API key pepper) |
| Image | ECR repository |
| AI repair | The task role may call Amazon Nova on Bedrock. It stays off until `ai_repair_enabled = true` |

## Before you start
- Install Terraform (1.5 or newer), Docker and the AWS CLI.
- Sign in to AWS from your terminal. Use a profile or the portal's temporary keys. Never write keys into these files.

## Deploy
```
cd infra
terraform init
terraform apply
```
The first apply creates everything except running containers, because the image does not exist yet. Then push the image:
```
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <ecr_repository_url without the repo name>
docker build -t <ecr_repository_url>:latest ..
docker push <ecr_repository_url>:latest
```
`ecr_repository_url` is printed by the first apply. Then start the services:
```
terraform apply -var deploy_services=true
```
Terraform prints `api_url` when it finishes.

## Fill in the secrets
The secrets are created with empty placeholders. Put the real values in the console or with the CLI. Never in a `.tf` file.
```
aws secretsmanager put-secret-value --secret-id doc-extract-dev/azure-docintel --secret-string '{"AZURE_DOCINTEL_ENDPOINT":"...","AZURE_DOCINTEL_KEY":"..."}'
aws secretsmanager put-secret-value --secret-id doc-extract-dev/app --secret-string '{"API_KEY_PEPPER":"a long random string"}'
```
Restart the services after changing a secret.

## Remove everything
```
terraform destroy
```

## Notes
- The worker command `python -m engine.worker` is built in Task 5. Until then, keep `deploy_services = false`, or the worker container will stop at start-up.
- HTTP only until you set `certificate_arn` to an ACM certificate. Then HTTP redirects to HTTPS.
- The containers run in the default VPC with public IPs to avoid the cost of a NAT gateway. For production, use private subnets.
- The alarm on the dead-letter queue has no notification yet. Add an SNS topic to it later.
