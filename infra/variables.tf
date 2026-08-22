variable "project" {
  description = "Name prefix for every resource, and the S3 bucket stem."
  type        = string
  default     = "regex-matching"

  validation {
    condition     = can(regex("^[a-z0-9-]{3,32}$", var.project))
    error_message = "Lowercase letters, digits and hyphens only (it becomes an S3 bucket name)."
  }
}

variable "region" {
  description = "AWS region. us-east-1 is the cheapest for this instance family."
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = <<-EOT
    EC2 instance type. t3.large (2 vCPU / 8 GB) is the practical minimum: the
    memory floor is set by the Spark JVM plus Postgres, Redis and gunicorn all
    running at once, and does not fall with traffic. t3.xlarge if you want
    headroom for the 2M-row demo without tuning Spark memory down.
  EOT
  type        = string
  default     = "t3.large"
}

variable "disk_gb" {
  description = "Root volume size. ~5 GB of images, plus Spark scratch and the generated seed file."
  type        = number
  default     = 40
}

variable "admin_cidrs" {
  description = <<-EOT
    CIDRs allowed to SSH. Set this to your own IP as "x.x.x.x/32" -- the default
    is intentionally empty so a wide-open SSH port cannot happen by accident.
    Find it with: curl -s https://checkip.amazonaws.com
  EOT
  type        = list(string)
  default     = []
}

variable "flower_cidrs" {
  description = <<-EOT
    CIDRs allowed to reach Flower on :5555. It has HTTP basic auth, but task
    metadata still does not need to be world-readable. Widen this to
    ["0.0.0.0/0"] if a reviewer needs to see the monitoring UI from anywhere.
  EOT
  type        = list(string)
  default     = []
}

variable "public_key_path" {
  description = "Path to an existing SSH public key. Leave empty to have Terraform generate a keypair and write the private key next to the state."
  type        = string
  default     = ""
}

variable "budget_alert_email" {
  description = "Email for AWS Budgets alerts. Empty disables the budget."
  type        = string
  default     = ""
}

variable "budget_limit_usd" {
  description = "Monthly budget that triggers the alerts above."
  type        = number
  default     = 80
}

variable "enable_cdn" {
  description = <<-EOT
    Put CloudFront in front of the instance to get a real HTTPS URL on a
    *.cloudfront.net hostname, with no domain to buy and no certificate to
    manage. Effectively free at this traffic level (1 TB/month always-free).
    Set to false to serve plain HTTP straight off the instance IP.
  EOT
  type        = bool
  default     = true
}
