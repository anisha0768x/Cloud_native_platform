variable "aws_region" {
  type    = string
  default = "ap-south-1"
}
variable "name" {
  type    = string
  default = "helio-production"
}
variable "image_tag" {
  type    = string
  default = "bootstrap"
}
variable "domain_name" {
  type    = string
  default = ""
}
variable "route53_zone_id" {
  description = "Optional existing Route 53 hosted zone ID for the production alias record."
  type        = string
  default     = ""
}
variable "certificate_arn" {
  type    = string
  default = ""
  validation {
    condition     = var.certificate_arn != "" || var.domain_name == ""
    error_message = "certificate_arn is required when domain_name is set."
  }
}
variable "ses_from" {
  type    = string
  default = ""
}
variable "ses_to" {
  type    = string
  default = ""
}
variable "db_instance_class" {
  type    = string
  default = "db.t4g.small"
}
variable "db_multi_az" {
  type    = bool
  default = true
}
variable "deletion_protection" {
  type    = bool
  default = true
}
variable "force_destroy_nonproduction" {
  type    = bool
  default = false
}
variable "gateway_desired_count" {
  type    = number
  default = 2
}
variable "service_desired_count" {
  type    = number
  default = 1
}
variable "log_retention_days" {
  type    = number
  default = 30
}
variable "enable_waf" {
  type    = bool
  default = true
}
variable "bootstrap_complete" {
  description = "Enable application services only after the one-shot bootstrap task succeeds."
  type        = bool
  default     = false
}
