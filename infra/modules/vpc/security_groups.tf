# Component 3 — the six platform security groups.
#
#   sg-alb        Inbound 443 from 0.0.0.0/0;        Outbound to sg-kong
#   sg-kong       Inbound from sg-alb;               Outbound to sg-eks-nodes
#   sg-eks-nodes  Inbound from sg-kong + self;       Outbound 443 (AWS APIs)
#   sg-rds        Inbound 5432 from sg-eks-nodes;     (no broad egress)
#   sg-valkey     Inbound 6379 from sg-eks-nodes;     (no broad egress)
#   sg-kafka      Inbound 9092,9094 from sg-eks-nodes;(no broad egress)
#
# Rules are authored as standalone aws_vpc_security_group_*_rule resources so
# the cross-SG references do not create a dependency cycle and each rule is
# independently planneable.

# --------------------------------------------------------------------------
# sg-alb — public internet edge (ALB)
# --------------------------------------------------------------------------
resource "aws_security_group" "alb" {
  name        = "${local.name}-sg-alb"
  description = "ALB edge: 443 from internet, egress to Kong"
  vpc_id      = module.vpc.vpc_id

  tags = merge(local.common_tags, { Name = "${local.name}-sg-alb" })
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from internet"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_kong" {
  security_group_id            = aws_security_group.alb.id
  description                  = "ALB -> Kong (plaintext HTTP inside VPC, intentional)"
  referenced_security_group_id = aws_security_group.kong.id
  from_port                    = 8000
  to_port                      = 8000
  ip_protocol                  = "tcp"
}

# --------------------------------------------------------------------------
# sg-kong — Kong API gateway pods (ingress namespace)
# --------------------------------------------------------------------------
# NOTE (do NOT "fix"): ALB -> Kong is plain HTTP (8000) inside the VPC private
# network. Acceptable per Component 8: sg-kong only accepts traffic from sg-alb,
# and the path traverses only AWS-managed infra inside the VPC. Kong -> backend
# is mTLS via Istio. Removing this boundary breaks the deploy.
resource "aws_security_group" "kong" {
  name        = "${local.name}-sg-kong"
  description = "Kong: inbound from ALB, egress to EKS nodes"
  vpc_id      = module.vpc.vpc_id

  tags = merge(local.common_tags, { Name = "${local.name}-sg-kong" })
}

resource "aws_vpc_security_group_ingress_rule" "kong_from_alb" {
  security_group_id            = aws_security_group.kong.id
  description                  = "Kong HTTP from ALB (plaintext, intentional)"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 8000
  to_port                      = 8000
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "kong_to_nodes" {
  security_group_id            = aws_security_group.kong.id
  description                  = "Kong -> EKS nodes (backend services via Istio)"
  referenced_security_group_id = aws_security_group.eks_nodes.id
  from_port                    = 0
  to_port                      = 65535
  ip_protocol                  = "tcp"
}

# --------------------------------------------------------------------------
# sg-eks-nodes — EKS worker nodes
# --------------------------------------------------------------------------
resource "aws_security_group" "eks_nodes" {
  name        = "${local.name}-sg-eks-nodes"
  description = "EKS nodes: inbound from Kong + self, egress 443 to AWS APIs"
  vpc_id      = module.vpc.vpc_id

  tags = merge(local.common_tags, {
    Name                                        = "${local.name}-sg-eks-nodes"
    "kubernetes.io/cluster/${var.cluster_name}" = "owned"
  })
}

resource "aws_vpc_security_group_ingress_rule" "nodes_from_kong" {
  security_group_id            = aws_security_group.eks_nodes.id
  description                  = "Node traffic from Kong"
  referenced_security_group_id = aws_security_group.kong.id
  from_port                    = 0
  to_port                      = 65535
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "nodes_from_self" {
  security_group_id            = aws_security_group.eks_nodes.id
  description                  = "Node-to-node (pod-to-pod, kubelet, CNI)"
  referenced_security_group_id = aws_security_group.eks_nodes.id
  ip_protocol                  = "-1"
}

resource "aws_vpc_security_group_egress_rule" "nodes_https_out" {
  security_group_id = aws_security_group.eks_nodes.id
  description       = "Egress 443 to AWS APIs / control plane / ECR / NAT"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

# DNS + intra-VPC egress so nodes can reach RDS/Valkey/MSK and CoreDNS.
resource "aws_vpc_security_group_egress_rule" "nodes_dns_udp" {
  security_group_id = aws_security_group.eks_nodes.id
  description       = "DNS (UDP) within VPC"
  cidr_ipv4         = var.vpc_cidr
  from_port         = 53
  to_port           = 53
  ip_protocol       = "udp"
}

resource "aws_vpc_security_group_egress_rule" "nodes_vpc_tcp" {
  security_group_id = aws_security_group.eks_nodes.id
  description       = "Intra-VPC TCP (data plane: RDS 5432, Valkey 6379, Kafka 9092/9094)"
  cidr_ipv4         = var.vpc_cidr
  from_port         = 0
  to_port           = 65535
  ip_protocol       = "tcp"
}

# --------------------------------------------------------------------------
# sg-rds — PostgreSQL (RDS), private
# --------------------------------------------------------------------------
resource "aws_security_group" "rds" {
  name        = "${local.name}-sg-rds"
  description = "RDS PostgreSQL: 5432 from EKS nodes only"
  vpc_id      = module.vpc.vpc_id

  tags = merge(local.common_tags, { Name = "${local.name}-sg-rds" })
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_nodes" {
  security_group_id            = aws_security_group.rds.id
  description                  = "PostgreSQL 5432 from EKS nodes"
  referenced_security_group_id = aws_security_group.eks_nodes.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

# --------------------------------------------------------------------------
# sg-valkey — ElastiCache (Valkey), private
# --------------------------------------------------------------------------
resource "aws_security_group" "valkey" {
  name        = "${local.name}-sg-valkey"
  description = "Valkey ElastiCache: 6379 from EKS nodes only"
  vpc_id      = module.vpc.vpc_id

  tags = merge(local.common_tags, { Name = "${local.name}-sg-valkey" })
}

resource "aws_vpc_security_group_ingress_rule" "valkey_from_nodes" {
  security_group_id            = aws_security_group.valkey.id
  description                  = "Valkey 6379 from EKS nodes"
  referenced_security_group_id = aws_security_group.eks_nodes.id
  from_port                    = 6379
  to_port                      = 6379
  ip_protocol                  = "tcp"
}

# --------------------------------------------------------------------------
# sg-kafka — MSK, private
# --------------------------------------------------------------------------
resource "aws_security_group" "kafka" {
  name        = "${local.name}-sg-kafka"
  description = "MSK Kafka: 9092 (TLS) + 9094 (SASL_SSL) from EKS nodes only"
  vpc_id      = module.vpc.vpc_id

  tags = merge(local.common_tags, { Name = "${local.name}-sg-kafka" })
}

resource "aws_vpc_security_group_ingress_rule" "kafka_9092_from_nodes" {
  security_group_id            = aws_security_group.kafka.id
  description                  = "Kafka 9092 from EKS nodes"
  referenced_security_group_id = aws_security_group.eks_nodes.id
  from_port                    = 9092
  to_port                      = 9092
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "kafka_9094_from_nodes" {
  security_group_id            = aws_security_group.kafka.id
  description                  = "Kafka 9094 (SASL_SSL) from EKS nodes"
  referenced_security_group_id = aws_security_group.eks_nodes.id
  from_port                    = 9094
  to_port                      = 9094
  ip_protocol                  = "tcp"
}
