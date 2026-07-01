output "vpc_id" {
  description = "ID of the VPC."
  value       = module.vpc.vpc_id
}

output "vpc_cidr" {
  description = "CIDR block of the VPC."
  value       = module.vpc.vpc_cidr_block
}

output "private_subnet_ids" {
  description = "IDs of the private subnets (EKS nodes, RDS, MSK, ElastiCache)."
  value       = module.vpc.private_subnets
}

output "public_subnet_ids" {
  description = "IDs of the public subnets (ALB, NAT gateways)."
  value       = module.vpc.public_subnets
}

output "nat_gateway_ids" {
  description = "IDs of the NAT gateways (one per AZ unless single_nat_gateway)."
  value       = module.vpc.natgw_ids
}

output "azs" {
  description = "Availability zones in use."
  value       = var.azs
}

output "sg_alb_id" {
  description = "ID of sg-alb (ALB edge)."
  value       = aws_security_group.alb.id
}

output "sg_kong_id" {
  description = "ID of sg-kong (Kong gateway)."
  value       = aws_security_group.kong.id
}

output "sg_eks_nodes_id" {
  description = "ID of sg-eks-nodes (worker nodes). Pass to the eks module."
  value       = aws_security_group.eks_nodes.id
}

output "sg_rds_id" {
  description = "ID of sg-rds (PostgreSQL)."
  value       = aws_security_group.rds.id
}

output "sg_valkey_id" {
  description = "ID of sg-valkey (ElastiCache)."
  value       = aws_security_group.valkey.id
}

output "sg_kafka_id" {
  description = "ID of sg-kafka (MSK)."
  value       = aws_security_group.kafka.id
}
