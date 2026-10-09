locals {
  services = toset([
    "gateway", "auth", "catalog", "metrics", "capacity", "logs", "forecast",
    "maintenance", "notifications", "storage", "configuration", "dashboard"
  ])
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

check "production_inputs" {
  assert {
    condition     = !var.bootstrap_complete || (var.domain_name != "" && var.certificate_arn != "")
    error_message = "domain_name and certificate_arn are required before application services are enabled."
  }
  assert {
    condition     = !var.bootstrap_complete || (var.ses_from != "" && var.ses_to != "")
    error_message = "ses_from and ses_to are required before application services are enabled."
  }
}

resource "aws_vpc" "main" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags                 = { Name = var.name }
}

resource "aws_internet_gateway" "main" { vpc_id = aws_vpc.main.id }

resource "aws_subnet" "public" {
  for_each                = { for i, az in local.azs : az => i }
  vpc_id                  = aws_vpc.main.id
  availability_zone       = each.key
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, each.value)
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.name}-public-${each.value + 1}" }
}

resource "aws_subnet" "private" {
  for_each          = { for i, az in local.azs : az => i }
  vpc_id            = aws_vpc.main.id
  availability_zone = each.key
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, each.value + 10)
  tags              = { Name = "${var.name}-private-${each.value + 1}" }
}

resource "aws_eip" "nat" { domain = "vpc" }
resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = values(aws_subnet.public)[0].id
  depends_on    = [aws_internet_gateway.main]
}

resource "aws_route_table" "public" { vpc_id = aws_vpc.main.id }
resource "aws_route" "public" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.main.id
}
resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}
resource "aws_route_table" "private" { vpc_id = aws_vpc.main.id }
resource "aws_route" "private" {
  route_table_id         = aws_route_table.private.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.main.id
}
resource "aws_route_table_association" "private" {
  for_each       = aws_subnet.private
  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}

resource "aws_security_group" "alb" {
  name_prefix = "${var.name}-alb-"
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "tasks" {
  name_prefix = "${var.name}-tasks-"
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "data" {
  name_prefix = "${var.name}-data-"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}
resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  count             = var.certificate_arn == "" ? 0 : 1
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}
resource "aws_vpc_security_group_egress_rule" "alb_tasks" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = 8000
  to_port                      = 8000
  ip_protocol                  = "tcp"
}
resource "aws_vpc_security_group_ingress_rule" "tasks_alb" {
  security_group_id            = aws_security_group.tasks.id
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 8000
  to_port                      = 8000
  ip_protocol                  = "tcp"
}
resource "aws_vpc_security_group_ingress_rule" "tasks_http" {
  security_group_id            = aws_security_group.tasks.id
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = 8000
  to_port                      = 8000
  ip_protocol                  = "tcp"
}
resource "aws_vpc_security_group_ingress_rule" "tasks_workload" {
  security_group_id            = aws_security_group.tasks.id
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = 9090
  to_port                      = 9091
  ip_protocol                  = "tcp"
}
resource "aws_vpc_security_group_egress_rule" "tasks_all" {
  security_group_id = aws_security_group.tasks.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
resource "aws_vpc_security_group_ingress_rule" "data_postgres" {
  security_group_id            = aws_security_group.data.id
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}
resource "aws_vpc_security_group_ingress_rule" "data_msk" {
  security_group_id            = aws_security_group.data.id
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = 9098
  to_port                      = 9098
  ip_protocol                  = "tcp"
}

resource "aws_ecr_repository" "platform" {
  name                 = "${var.name}/platform"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}
resource "aws_ecr_repository" "workload" {
  name                 = "${var.name}/workload"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_s3_bucket" "objects" {
  bucket_prefix = "${var.name}-objects-"
  force_destroy = var.force_destroy_nonproduction
}
resource "aws_s3_bucket_versioning" "objects" {
  bucket = aws_s3_bucket.objects.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "objects" {
  bucket = aws_s3_bucket.objects.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
resource "aws_s3_bucket_public_access_block" "objects" {
  bucket                  = aws_s3_bucket.objects.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_lifecycle_configuration" "objects" {
  bucket = aws_s3_bucket.objects.id
  rule {
    id     = "noncurrent-retention"
    status = "Enabled"
    noncurrent_version_expiration { noncurrent_days = 90 }
  }
}

resource "random_password" "db_master" {
  length  = 32
  special = false
}
resource "random_password" "db_service" {
  for_each = local.services
  length   = 32
  special  = false
}
resource "random_password" "internal" {
  length  = 48
  special = false
}
resource "random_password" "setup" {
  length  = 32
  special = false
}
resource "random_id" "auth_key" { byte_length = 32 }
resource "random_id" "storage_key" { byte_length = 32 }

resource "aws_db_subnet_group" "main" {
  name       = var.name
  subnet_ids = values(aws_subnet.private)[*].id
}
resource "aws_db_instance" "postgres" {
  identifier                   = var.name
  engine                       = "postgres"
  engine_version               = "17"
  instance_class               = var.db_instance_class
  allocated_storage            = 50
  max_allocated_storage        = 250
  storage_encrypted            = true
  db_name                      = "postgres"
  username                     = "helio_admin"
  password                     = random_password.db_master.result
  multi_az                     = var.db_multi_az
  db_subnet_group_name         = aws_db_subnet_group.main.name
  vpc_security_group_ids       = [aws_security_group.data.id]
  backup_retention_period      = 14
  deletion_protection          = var.deletion_protection
  skip_final_snapshot          = false
  final_snapshot_identifier    = "${var.name}-final"
  performance_insights_enabled = true
  apply_immediately            = false
}

resource "aws_msk_serverless_cluster" "events" {
  cluster_name = "${var.name}-events"
  vpc_config {
    subnet_ids         = values(aws_subnet.private)[*].id
    security_group_ids = [aws_security_group.data.id]
  }
  client_authentication {
    sasl {
      iam {
        enabled = true
      }
    }
  }
}

resource "aws_secretsmanager_secret" "service" {
  for_each                = local.services
  name                    = "${var.name}/${each.key}"
  recovery_window_in_days = 30
}
resource "aws_secretsmanager_secret_version" "service" {
  for_each  = local.services
  secret_id = aws_secretsmanager_secret.service[each.key].id
  secret_string = jsonencode({
    DATABASE_URL           = "postgresql://helio_${each.key}:${random_password.db_service[each.key].result}@${aws_db_instance.postgres.address}:5432/helio_${each.key}?sslmode=require"
    INTERNAL_SECRET        = random_password.internal.result
    SETUP_TOKEN            = each.key == "auth" ? random_password.setup.result : "unused"
    AUTH_ENCRYPTION_KEY    = each.key == "auth" ? random_id.auth_key.b64_std : "unused"
    STORAGE_ENCRYPTION_KEY = each.key == "storage" ? random_id.storage_key.b64_std : "unused"
  })
}
resource "aws_secretsmanager_secret" "bootstrap" {
  name                    = "${var.name}/database-bootstrap"
  recovery_window_in_days = 30
}
resource "aws_secretsmanager_secret_version" "bootstrap" {
  secret_id = aws_secretsmanager_secret.bootstrap.id
  secret_string = jsonencode({
    MASTER_DATABASE_URL = "postgresql://helio_admin:${random_password.db_master.result}@${aws_db_instance.postgres.address}:5432/postgres?sslmode=require"
    SERVICE_PASSWORDS   = jsonencode({ for name, value in random_password.db_service : name => value.result })
  })
}

resource "aws_ecs_cluster" "main" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enhanced"
  }
}
resource "aws_service_discovery_private_dns_namespace" "main" {
  name = "helio.internal"
  vpc  = aws_vpc.main.id
}
resource "aws_cloudwatch_log_group" "service" {
  for_each          = local.services
  name              = "/ecs/${var.name}/${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy" "execution_secrets" {
  role   = aws_iam_role.execution.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = concat(values(aws_secretsmanager_secret.service)[*].arn, [aws_secretsmanager_secret.bootstrap.arn]) }] })
}
resource "aws_iam_role" "task" {
  for_each           = local.services
  name               = substr("${var.name}-${each.key}-task", 0, 64)
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_iam_role_policy" "task" {
  for_each = local.services
  role     = aws_iam_role.task[each.key].id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"], Resource = aws_msk_serverless_cluster.events.arn },
    { Effect = "Allow", Action = ["kafka-cluster:ReadData", "kafka-cluster:WriteData", "kafka-cluster:DescribeTopic"], Resource = "${replace(aws_msk_serverless_cluster.events.arn, ":cluster/", ":topic/")}/*" },
    { Effect = "Allow", Action = ["kafka-cluster:AlterGroup", "kafka-cluster:DescribeGroup"], Resource = "${replace(aws_msk_serverless_cluster.events.arn, ":cluster/", ":group/")}/*" },
    ], each.key == "storage" ? [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"], Resource = [aws_s3_bucket.objects.arn, "${aws_s3_bucket.objects.arn}/*"] }
    ] : [], each.key == "capacity" ? [
    { Effect = "Allow", Action = ["ecs:DescribeServices", "ecs:UpdateService"], Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/${aws_ecs_cluster.main.name}/${var.name}-workload" }
    ] : [], each.key == "notifications" ? [
    { Effect = "Allow", Action = ["ses:SendEmail"], Resource = "*" }
  ] : []) })
}
resource "aws_iam_role" "bootstrap" {
  name               = substr("${var.name}-bootstrap-task", 0, 64)
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_iam_role_policy" "bootstrap" {
  role = aws_iam_role.bootstrap.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"], Resource = aws_msk_serverless_cluster.events.arn },
    { Effect = "Allow", Action = ["kafka-cluster:CreateTopic", "kafka-cluster:DescribeTopic"], Resource = "${replace(aws_msk_serverless_cluster.events.arn, ":cluster/", ":topic/")}/*" }
  ] })
}

resource "aws_lb" "gateway" {
  name               = substr(replace(var.name, "_", "-"), 0, 32)
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = values(aws_subnet.public)[*].id
}
resource "aws_lb_target_group" "gateway" {
  name        = substr("${replace(var.name, "_", "-")}-gw", 0, 32)
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id
  health_check {
    path              = "/health"
    matcher           = "200"
    healthy_threshold = 2
  }
}
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.gateway.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = var.certificate_arn == "" ? "forward" : "redirect"
    target_group_arn = var.certificate_arn == "" ? aws_lb_target_group.gateway.arn : null
    dynamic "redirect" {
      for_each = var.certificate_arn == "" ? [] : [1]
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}
resource "aws_lb_listener" "https" {
  count             = var.certificate_arn == "" ? 0 : 1
  load_balancer_arn = aws_lb.gateway.arn
  port              = 443
  protocol          = "HTTPS"
  certificate_arn   = var.certificate_arn
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
  }
}

resource "aws_route53_record" "gateway" {
  count   = var.route53_zone_id == "" ? 0 : 1
  zone_id = var.route53_zone_id
  name    = var.domain_name
  type    = "A"
  alias {
    name                   = aws_lb.gateway.dns_name
    zone_id                = aws_lb.gateway.zone_id
    evaluate_target_health = true
  }
}

resource "aws_wafv2_web_acl" "gateway" {
  count = var.enable_waf ? 1 : 0
  name  = "${var.name}-gateway"
  scope = "REGIONAL"
  default_action {
    allow {}
  }
  rule {
    name     = "aws-common-rules"
    priority = 10
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-common-rules"
      sampled_requests_enabled   = true
    }
  }
  rule {
    name     = "per-ip-rate-limit"
    priority = 20
    action {
      block {}
    }
    statement {
      rate_based_statement {
        aggregate_key_type = "IP"
        limit              = 2000
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-rate-limit"
      sampled_requests_enabled   = true
    }
  }
  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.name}-gateway"
    sampled_requests_enabled   = true
  }
}
resource "aws_wafv2_web_acl_association" "gateway" {
  count        = var.enable_waf ? 1 : 0
  resource_arn = aws_lb.gateway.arn
  web_acl_arn  = aws_wafv2_web_acl.gateway[0].arn
}

resource "aws_ecs_task_definition" "service" {
  for_each                 = local.services
  family                   = "${var.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task[each.key].arn
  container_definitions = jsonencode([{
    name                   = each.key
    image                  = "${aws_ecr_repository.platform.repository_url}:${var.image_tag}"
    essential              = true
    readonlyRootFilesystem = true
    portMappings           = [{ name = "http", containerPort = 8000, protocol = "tcp", appProtocol = "http" }]
    environment = concat([
      { name = "HELIO_SERVICE", value = each.key },
      { name = "AWS_REGION", value = var.aws_region },
      { name = "KAFKA_BOOTSTRAP", value = aws_msk_serverless_cluster.events.bootstrap_brokers_sasl_iam },
      { name = "KAFKA_SECURITY_PROTOCOL", value = "SASL_SSL" },
      { name = "KAFKA_SASL_MECHANISM", value = "OAUTHBEARER" },
      { name = "SERVICE_URL_TEMPLATE", value = "http://{service}.helio.internal:8000" },
      { name = "DB_POOL_MIN", value = "1" },
      { name = "DB_POOL_MAX", value = "4" }
      ], each.key == "capacity" ? [
      { name = "RUNTIME", value = "ecs" },
      { name = "ECS_CLUSTER", value = aws_ecs_cluster.main.name },
      { name = "ECS_WORKLOAD_SERVICE", value = "${var.name}-workload" },
      { name = "ECS_WORKLOAD_URL", value = "http://workload.helio.internal:9090" }
      ] : [], each.key == "storage" ? [
      { name = "S3_BUCKET", value = aws_s3_bucket.objects.id },
      { name = "S3_REGION", value = var.aws_region },
      { name = "S3_AUTO_CREATE", value = "false" }
      ] : [], each.key == "notifications" ? [
      { name = "EMAIL_PROVIDER", value = "ses" },
      { name = "SES_FROM", value = var.ses_from },
      { name = "SES_TO", value = var.ses_to }
    ] : [])
    secrets = [for key in ["DATABASE_URL", "INTERNAL_SECRET", "SETUP_TOKEN", "AUTH_ENCRYPTION_KEY", "STORAGE_ENCRYPTION_KEY"] : {
      name = key, valueFrom = "${aws_secretsmanager_secret.service[each.key].arn}:${key}::"
    }]
    healthCheck      = { command = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/ready',timeout=3)\" || exit 1"], interval = 30, timeout = 5, retries = 3, startPeriod = 60 }
    logConfiguration = { logDriver = "awslogs", options = { "awslogs-group" = aws_cloudwatch_log_group.service[each.key].name, "awslogs-region" = var.aws_region, "awslogs-stream-prefix" = "service" } }
    linuxParameters  = { initProcessEnabled = true }
  }])
}

resource "aws_ecs_service" "service" {
  for_each                           = var.bootstrap_complete ? local.services : toset([])
  name                               = "${var.name}-${each.key}"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.service[each.key].arn
  desired_count                      = each.key == "gateway" ? var.gateway_desired_count : var.service_desired_count
  launch_type                        = "FARGATE"
  platform_version                   = "LATEST"
  enable_execute_command             = true
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  network_configuration {
    subnets          = values(aws_subnet.private)[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }
  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_private_dns_namespace.main.arn
    service {
      port_name      = "http"
      discovery_name = each.key
      client_alias {
        dns_name = each.key
        port     = 8000
      }
    }
  }
  dynamic "load_balancer" {
    for_each = each.key == "gateway" ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.gateway.arn
      container_name   = each.key
      container_port   = 8000
    }
  }
  depends_on = [aws_lb_listener.http]
}

resource "aws_ecs_task_definition" "workload" {
  family                   = "${var.name}-workload"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  container_definitions = jsonencode([{
    name             = "workload", image = "${aws_ecr_repository.workload.repository_url}:${var.image_tag}", essential = true,
    command          = ["python", "worker.py", "--health-port", "9091"], readonlyRootFilesystem = true,
    portMappings     = [{ name = "work", containerPort = 9090, protocol = "tcp", appProtocol = "http" }, { name = "health", containerPort = 9091, protocol = "tcp", appProtocol = "http" }],
    healthCheck      = { command = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:9091/health',timeout=3)\" || exit 1"], interval = 15, timeout = 5, retries = 3, startPeriod = 20 },
    logConfiguration = { logDriver = "awslogs", options = { "awslogs-group" = aws_cloudwatch_log_group.workload.name, "awslogs-region" = var.aws_region, "awslogs-stream-prefix" = "worker" } }
  }])
}
resource "aws_cloudwatch_log_group" "workload" {
  name              = "/ecs/${var.name}/workload"
  retention_in_days = var.log_retention_days
}
resource "aws_ecs_service" "workload" {
  count           = var.bootstrap_complete ? 1 : 0
  name            = "${var.name}-workload"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.workload.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  network_configuration {
    subnets          = values(aws_subnet.private)[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }
  service_connect_configuration {
    enabled   = true
    namespace = aws_service_discovery_private_dns_namespace.main.arn
    service {
      port_name      = "work"
      discovery_name = "workload"
      client_alias {
        dns_name = "workload"
        port     = 9090
      }
    }
  }
}

resource "aws_appautoscaling_target" "gateway" {
  count              = var.bootstrap_complete ? 1 : 0
  max_capacity       = 8
  min_capacity       = 2
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.service["gateway"].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}
resource "aws_appautoscaling_policy" "gateway_cpu" {
  count              = var.bootstrap_complete ? 1 : 0
  name               = "${var.name}-gateway-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.gateway[0].resource_id
  scalable_dimension = aws_appautoscaling_target.gateway[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.gateway[0].service_namespace
  target_tracking_scaling_policy_configuration {
    target_value = 60
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    scale_in_cooldown  = 120
    scale_out_cooldown = 60
  }
}

resource "aws_ecs_task_definition" "bootstrap" {
  family                   = "${var.name}-bootstrap"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.bootstrap.arn
  container_definitions = jsonencode([{
    name      = "bootstrap"
    image     = "${aws_ecr_repository.platform.repository_url}:${var.image_tag}"
    essential = true
    command   = ["python", "/opt/platform/aws_bootstrap.py"]
    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "KAFKA_BOOTSTRAP", value = aws_msk_serverless_cluster.events.bootstrap_brokers_sasl_iam },
      { name = "KAFKA_SECURITY_PROTOCOL", value = "SASL_SSL" },
      { name = "KAFKA_SASL_MECHANISM", value = "OAUTHBEARER" }
    ]
    secrets = [
      { name = "MASTER_DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.bootstrap.arn}:MASTER_DATABASE_URL::" },
      { name = "SERVICE_PASSWORDS", valueFrom = "${aws_secretsmanager_secret.bootstrap.arn}:SERVICE_PASSWORDS::" }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.bootstrap.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "bootstrap"
      }
    }
  }])
}
resource "aws_cloudwatch_log_group" "bootstrap" {
  name              = "/ecs/${var.name}/bootstrap"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_metric_alarm" "gateway_5xx" {
  alarm_name          = "${var.name}-gateway-5xx"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Sum"
  threshold           = 5
  treat_missing_data  = "notBreaching"
  dimensions          = { LoadBalancer = aws_lb.gateway.arn_suffix, TargetGroup = aws_lb_target_group.gateway.arn_suffix }
}
