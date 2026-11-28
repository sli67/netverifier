terraform {
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.0" }
    http   = { source = "hashicorp/http", version = "~> 3.4" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

variable "region" { default = "us-east-2" }       # your project's selected Region
variable "instance_type" { default = "t3.small" } # app server: 2 vCPU, 2 GB RAM (free-plan eligible)
variable "worker_type" { default = "t3.small" }   # worker + Batfish; c7i-flex.large (4 GB) if Batfish runs out of memory
variable "public_key" { default = "~/.ssh/id_ed25519.pub" }

provider "aws" { region = var.region }

data "aws_caller_identity" "me" {}

# Your current public IP, so only this machine can reach the server
data "http" "my_ip" { url = "https://checkip.amazonaws.com" }
locals { my_ip = chomp(data.http.my_ip.response_body) }

# ───────── Network: one VPC, one public subnet ─────────
resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  tags                 = { Name = "verifier" }
}

resource "aws_internet_gateway" "main" { vpc_id = aws_vpc.main.id }

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = true
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# App server: SSH and the dashboard (Traefik on port 80) from your IP
resource "aws_security_group" "main" {
  vpc_id = aws_vpc.main.id

  dynamic "ingress" {
    for_each = [22, 80]
    content {
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = ["${local.my_ip}/32"]
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Worker server: SSH from your IP only; it makes outbound connections only
resource "aws_security_group" "worker" {
  vpc_id = aws_vpc.main.id

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["${local.my_ip}/32"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# k3s cluster traffic between the two servers (API 6443, kubelet, flannel VXLAN, image registry 5000)
resource "aws_security_group" "cluster" {
  vpc_id = aws_vpc.main.id

  ingress {
    from_port = 0
    to_port   = 0
    protocol  = "-1"
    self      = true
  }
}

resource "random_password" "k3s_token" {
  length  = 48
  special = false
}

# ───────── Database: self-hosted Postgres in k3s (k8s/verifier.yaml) ─────────
# Terraform only makes the password; the deploy script hands it to the Postgres pod.
resource "random_password" "db" {
  length  = 32
  special = false # keeps DATABASE_URL valid without URL-encoding
}

resource "aws_ssm_parameter" "db_password" {
  name  = "/verifier/db-password"
  type  = "SecureString"
  value = random_password.db.result
}

# Cluster-internal address of the Postgres Service
resource "aws_ssm_parameter" "database_url" {
  name  = "/verifier/database-url"
  type  = "SecureString"
  value = "postgres://verifier:${random_password.db.result}@postgres.verifier.svc.cluster.local:5432/verifier"
}

# ───────── Storage: snapshot bucket (use instead of S3Mock) ─────────
resource "aws_s3_bucket" "main" {
  bucket = "verifier-${data.aws_caller_identity.me.account_id}"
}

resource "aws_s3_bucket_public_access_block" "main" {
  bucket                  = aws_s3_bucket.main.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ───────── Instance role: the bucket and the database URL, no access keys needed ─────────
resource "aws_iam_role" "app" {
  name = "verifier-app"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "app" {
  role = aws_iam_role.app.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = "${aws_s3_bucket.main.arn}/*" },
      { Effect = "Allow", Action = "s3:ListBucket", Resource = aws_s3_bucket.main.arn },
      { Effect = "Allow", Action = "ssm:GetParameter", Resource = [aws_ssm_parameter.database_url.arn, aws_ssm_parameter.db_password.arn] },
    ]
  })
}

resource "aws_iam_instance_profile" "app" {
  name = "verifier-app"
  role = aws_iam_role.app.name
}

# ───────── Servers: a two-node k3s cluster ─────────
resource "aws_key_pair" "me" {
  key_name   = "verifier"
  public_key = file(pathexpand(var.public_key))
}

locals {
  # 4 GB swap: 2 GB of RAM is tight
  swap = <<-EOF
    dd if=/dev/zero of=/swapfile bs=1M count=4096
    chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
    echo '/swapfile none swap defaults 0 0' >> /etc/fstab
  EOF

  # Both nodes pull registry.verifier.internal/* from the in-cluster registry on the app node
  registries = <<-EOF
    mkdir -p /etc/rancher/k3s
    cat > /etc/rancher/k3s/registries.yaml <<YAML
    mirrors:
      registry.verifier.internal:
        endpoint: ["http://REGISTRY:5000"]
    YAML
  EOF
}

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

# App node: k3s server (control plane), API, dashboard, image registry; Docker for building images
resource "aws_instance" "app" {
  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.main.id, aws_security_group.cluster.id]
  key_name                    = aws_key_pair.me.key_name
  iam_instance_profile        = aws_iam_instance_profile.app.name
  user_data_replace_on_change = true

  # Hop limit 2 lets pods reach instance metadata, so they use the instance role for S3
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = <<-EOF
    #!/bin/bash
    set -eux
    ${local.swap}
    dnf install -y docker git
    systemctl enable --now docker
    usermod -aG docker ec2-user

    mkdir -p /etc/verifier
    cat > /etc/verifier/env <<ENV
    S3_BUCKET=${aws_s3_bucket.main.id}
    S3_REGION=${var.region}
    ENV

    ${replace(local.registries, "REGISTRY", "127.0.0.1")}
    curl -sfL https://get.k3s.io | K3S_TOKEN=${random_password.k3s_token.result} \
      INSTALL_K3S_EXEC="server --node-label role=app" sh -

    # kubectl for ec2-user
    mkdir -p /home/ec2-user/.kube
    cp /etc/rancher/k3s/k3s.yaml /home/ec2-user/.kube/config
    chown -R ec2-user:ec2-user /home/ec2-user/.kube
    chmod 600 /home/ec2-user/.kube/config
    echo 'export KUBECONFIG=$HOME/.kube/config' >> /home/ec2-user/.bashrc
  EOF

  tags = { Name = "verifier-app" }
}

# Worker node: k3s agent; runs the agent worker and its Batfish
resource "aws_instance" "worker" {
  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = var.worker_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.worker.id, aws_security_group.cluster.id]
  key_name                    = aws_key_pair.me.key_name
  iam_instance_profile        = aws_iam_instance_profile.app.name
  user_data_replace_on_change = true

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = <<-EOF
    #!/bin/bash
    set -eux
    ${local.swap}
    ${replace(local.registries, "REGISTRY", aws_instance.app.private_ip)}
    # The agent keeps retrying until the server is up
    curl -sfL https://get.k3s.io | K3S_URL=https://${aws_instance.app.private_ip}:6443 \
      K3S_TOKEN=${random_password.k3s_token.result} INSTALL_K3S_EXEC="agent --node-label role=worker" sh -
  EOF

  tags = { Name = "verifier-worker" }
}

output "ssh_app" { value = "ssh ec2-user@${aws_instance.app.public_ip}" }
output "ssh_worker" { value = "ssh ec2-user@${aws_instance.worker.public_ip}" }
output "app_public_ip" { value = aws_instance.app.public_ip }
output "url" { value = "http://${aws_instance.app.public_ip}" }
output "database_url" {
  value     = aws_ssm_parameter.database_url.value
  sensitive = true
}
output "bucket" { value = aws_s3_bucket.main.id }
