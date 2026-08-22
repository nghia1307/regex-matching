output "app_url" {
  description = "The link to send out."
  value       = "http://${aws_eip.app.public_ip}/"
}

output "flower_url" {
  description = "Task/worker monitoring, direct (HTTP basic auth; credentials come from deploy.sh). Also reachable through the CDN at cdn_url + \"flower/\"."
  value       = "http://${aws_eip.app.public_ip}:5555/flower/"
}

output "public_ip" {
  value = aws_eip.app.public_ip
}

output "s3_bucket" {
  description = "Real Amazon S3 bucket the deployed stack reads and writes."
  value       = aws_s3_bucket.data.id
}

output "region" {
  value = var.region
}

output "ssh_key_path" {
  description = "Private key for SSH. Terraform-generated unless you supplied your own."
  value       = var.public_key_path == "" ? abspath(local_file.private_key[0].filename) : "(using your own key)"
}

output "ssh_command" {
  value = "ssh -i ${var.public_key_path == "" ? "${var.project}-key.pem" : "<your-key>"} ubuntu@${aws_eip.app.public_ip}"
}

output "spark_ui_tunnel" {
  description = "The Spark UIs are bound to localhost on the box; forward them over SSH."
  value       = "ssh -i ${var.project}-key.pem -L 8080:localhost:8080 -L 4040:localhost:4040 ubuntu@${aws_eip.app.public_ip}"
}
output "project" {
  value = var.project
}

output "cdn_url" {
  description = "HTTPS front door. This is the link to send to reviewers."
  value       = var.enable_cdn ? "https://${aws_cloudfront_distribution.app[0].domain_name}/" : "(CDN disabled -- use app_url)"
}

output "cdn_domain" {
  description = "Bare CloudFront hostname; deploy.sh puts it in ALLOWED_HOSTS."
  value       = var.enable_cdn ? aws_cloudfront_distribution.app[0].domain_name : ""
}

output "instance_dns" {
  description = "AWS-assigned hostname for the Elastic IP."
  value       = aws_eip.app.public_dns
}
