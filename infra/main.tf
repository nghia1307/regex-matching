# =============================================================================
# One EC2 instance running the docker-compose stack, plus the S3 bucket it reads
# and an IAM instance profile so no AWS keys ever land on the box.
#
#   terraform init
#   terraform apply
#   ../deploy.sh
#
# Cost, kept deliberately inside a $100 credit (us-east-1, on-demand):
#   t3.large ......... ~$61 / month running continuously
#   40 GB gp3 EBS .... ~$3.20 / month
#   Elastic IP ....... free while attached to a running instance
#   S3 (~300 MB) ..... inside the always-free 5 GB allowance
#   -> ~$65 / month. No NAT gateway and no load balancer, which is most of why:
#      those two alone would add ~$50/month for a single-instance demo that does
#      not need either.
# =============================================================================

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
    tls    = { source = "hashicorp/tls", version = "~> 4.0" }
    local  = { source = "hashicorp/local", version = "~> 2.5" }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
    }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

resource "random_id" "suffix" {
  byte_length = 4
}

locals {
  name   = var.project
  bucket = "${var.project}-${random_id.suffix.hex}"
}

# --------------------------------------------------------------------------- #
# Network: one public subnet. The instance needs inbound HTTP from the world, so
# a private subnet would mean a NAT gateway (~$32/month) for no security gain
# over a tight security group on a single box.
# --------------------------------------------------------------------------- #
resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.20.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true
  tags                    = { Name = "${local.name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# --------------------------------------------------------------------------- #
# Security group: the app is public, everything operational is not.
# --------------------------------------------------------------------------- #
resource "aws_security_group" "app" {
  name        = "${local.name}-app"
  description = "Public app on 80; SSH and Flower restricted to admin CIDRs"
  vpc_id      = aws_vpc.main.id

  # The application itself -- this is the link you send out.
  ingress {
    description = "HTTP (app + /api)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Flower carries HTTP basic auth, but there is no reason to expose task
  # metadata more widely than the people reviewing it.
  ingress {
    description = "Flower (basic auth)"
    from_port   = 5555
    to_port     = 5555
    protocol    = "tcp"
    cidr_blocks = var.flower_cidrs
  }

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = var.admin_cidrs
  }

  egress {
    description = "all outbound (Gemini API, S3, package registries)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-app" }
}

# Note what is NOT here: 8080 (Spark master UI), 4040 (driver UI), 5432, 6379.
# Those are bound to 127.0.0.1 in docker-compose.prod.yml and reached over an
# SSH tunnel -- the Spark UIs have no authentication of their own.

# --------------------------------------------------------------------------- #
# S3: the data bucket, private, versioning off (test data is regenerable).
# --------------------------------------------------------------------------- #
resource "aws_s3_bucket" "data" {
  bucket        = local.bucket
  force_destroy = true # so `terraform destroy` does not strand a bucket
  tags          = { Name = local.bucket }
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

# Job output is disposable. Expiring it keeps a forgotten demo from quietly
# growing past the free tier.
resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    id     = "expire-job-results"
    status = "Enabled"
    filter {
      prefix = "results/"
    }
    expiration {
      days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# --------------------------------------------------------------------------- #
# IAM: an instance profile scoped to this one bucket. This is why the deployed
# stack has no AWS_ACCESS_KEY_ID at all -- boto3, pyarrow and Hadoop's S3A all
# fall through to the instance metadata credentials.
# --------------------------------------------------------------------------- #
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "bucket_access" {
  statement {
    sid       = "ListTheBucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.data.arn]
  }
  statement {
    sid = "ReadWriteObjects"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${aws_s3_bucket.data.arn}/*"]
  }
}

resource "aws_iam_role" "instance" {
  name               = "${local.name}-instance"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy" "bucket_access" {
  name   = "${local.name}-bucket-access"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.bucket_access.json
}

resource "aws_iam_instance_profile" "instance" {
  name = "${local.name}-instance"
  role = aws_iam_role.instance.name
}

# --------------------------------------------------------------------------- #
# SSH key: generated here and written to disk, so `terraform apply` is all that
# is needed to get in. Supply var.public_key_path to use your own instead.
# --------------------------------------------------------------------------- #
resource "tls_private_key" "generated" {
  count     = var.public_key_path == "" ? 1 : 0
  algorithm = "ED25519"
}

resource "local_file" "private_key" {
  count           = var.public_key_path == "" ? 1 : 0
  content         = tls_private_key.generated[0].private_key_openssh
  filename        = "${path.module}/${local.name}-key.pem"
  file_permission = "0600"
}

resource "aws_key_pair" "main" {
  key_name   = "${local.name}-key"
  public_key = var.public_key_path == "" ? tls_private_key.generated[0].public_key_openssh : file(var.public_key_path)
}

# --------------------------------------------------------------------------- #
# The instance
# --------------------------------------------------------------------------- #
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd*/ubuntu-noble-24.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_instance" "app" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name
  key_name               = aws_key_pair.main.key_name

  user_data                   = file("${path.module}/user_data.sh")
  user_data_replace_on_change = false # editing the script must not recycle the box

  root_block_device {
    volume_size           = var.disk_gb
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  metadata_options {
    http_tokens = "required" # IMDSv2 only
    # The default hop limit of 1 stops *containers* from reaching the metadata
    # service, because the Docker bridge is one extra hop. Without this, S3A and
    # boto3 inside the containers cannot pick up the instance-profile
    # credentials and every S3 call fails with 403.
    http_put_response_hop_limit = 2
    http_endpoint               = "enabled"
  }

  tags = { Name = local.name }
}

resource "aws_eip" "app" {
  instance = aws_instance.app.id
  domain   = "vpc"
  tags     = { Name = local.name }
}

# --------------------------------------------------------------------------- #
# Spend guardrail. The instance runs continuously, so the realistic failure mode
# is a forgotten stack, not a traffic spike. Set budget_alert_email to arm it.
# --------------------------------------------------------------------------- #
resource "aws_budgets_budget" "monthly" {
  count = var.budget_alert_email == "" ? 0 : 1

  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Warn at 80% of forecast and again when actual spend crosses the limit.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}

# --------------------------------------------------------------------------- #
# HTTPS via CloudFront.
#
# EC2 is IaaS: AWS hands over a VM and an IP, and TLS termination is the
# operator's problem. A certificate needs a domain you can prove you own, and
# nobody will issue one for the *.amazonaws.com name AWS assigns -- so the way
# to get real HTTPS without buying a domain is to put CloudFront in front. It
# supplies both halves: a *.cloudfront.net hostname and a valid certificate for
# it, at no cost.
#
# The distribution is a pure reverse proxy here, not a cache. This is an
# application with mutating endpoints, not a static site, so correctness beats
# hit rate: caching is disabled everywhere and every header, cookie and query
# string is passed straight through.
# --------------------------------------------------------------------------- #
data "aws_cloudfront_cache_policy" "disabled" {
  count = var.enable_cdn ? 1 : 0
  name  = "Managed-CachingDisabled"
}

# AllViewer forwards the viewer's Host header to the origin, so Django sees the
# CloudFront hostname -- which is why deploy.sh must add it to ALLOWED_HOSTS.
data "aws_cloudfront_origin_request_policy" "all_viewer" {
  count = var.enable_cdn ? 1 : 0
  name  = "Managed-AllViewer"
}

resource "aws_cloudfront_distribution" "app" {
  count = var.enable_cdn ? 1 : 0

  enabled         = true
  comment         = "${local.name} -- HTTPS front door"
  is_ipv6_enabled = true

  # PriceClass_All keeps the Australian and Asian edges in play. The cheaper
  # tiers exclude them, which would add a few hundred ms for reviewers in this
  # part of the world; the always-free 1 TB/month covers this either way.
  price_class = "PriceClass_All"

  origin {
    # An IP address is not a valid CloudFront origin, so this uses the DNS name
    # of the Elastic IP -- stable across instance reboots and replacements.
    domain_name = aws_eip.app.public_dns
    origin_id   = "ec2"

    custom_origin_config {
      http_port  = 80
      https_port = 443
      # The origin has no certificate of its own; TLS stops at CloudFront.
      # Fine for this deployment, and the alternative (a self-signed cert on
      # the box) buys nothing a reviewer can verify.
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]

      # Page reads are fast, but a cold Parquet read on a burstable instance can
      # take a few seconds; 60s is the ceiling without a service quota increase.
      # Long Spark jobs never hold a request open -- submit returns 202
      # immediately and the client polls -- so this is not the job timeout.
      origin_read_timeout      = 60
      origin_keepalive_timeout = 5
    }
  }

  default_cache_behavior {
    target_origin_id = "ec2"
    # Everything is redirected to HTTPS, so a reviewer pasting http:// still
    # lands on a secure connection.
    viewer_protocol_policy = "redirect-to-https"

    # POST is required to submit a job and to cancel one. CloudFront's default
    # is GET/HEAD only, which would make the app look read-only.
    allowed_methods = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods  = ["GET", "HEAD"]
    compress        = true

    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled[0].id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer[0].id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    # The free certificate that comes with the *.cloudfront.net hostname.
    cloudfront_default_certificate = true
    minimum_protocol_version       = "TLSv1"
  }

  tags = { Name = "${local.name}-cdn" }
}
