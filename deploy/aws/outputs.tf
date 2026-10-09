output "gateway_url" { value = var.certificate_arn == "" ? "http://${aws_lb.gateway.dns_name}" : "https://${var.domain_name != "" ? var.domain_name : aws_lb.gateway.dns_name}" }
output "platform_repository" { value = aws_ecr_repository.platform.repository_url }
output "workload_repository" { value = aws_ecr_repository.workload.repository_url }
output "ecs_cluster" { value = aws_ecs_cluster.main.name }
output "database_bootstrap_secret_arn" { value = aws_secretsmanager_secret.bootstrap.arn }
output "setup_token_secret_arn" {
  value     = aws_secretsmanager_secret.service["auth"].arn
  sensitive = true
}
output "bootstrap_task_definition" { value = aws_ecs_task_definition.bootstrap.arn }
output "private_subnet_ids" { value = values(aws_subnet.private)[*].id }
output "task_security_group_id" { value = aws_security_group.tasks.id }
