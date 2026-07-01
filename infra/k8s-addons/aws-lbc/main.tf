###############################################################################
# AWS Load Balancer Controller — Component 10
#
# Install method: Helm (eks/aws-load-balancer-controller).
# Purpose: watches K8s Service type:LoadBalancer / Ingress -> creates AWS ALBs
# automatically (Kong's proxy Service is the primary consumer — Component 8).
#
# IRSA: the controller ServiceAccount is bound to the AWSLoadBalancerControllerRole
# created in modules/iam (var.irsa_role_arn). Permissions: ec2:*,
# elasticloadbalancing:*, iam:CreateServiceLinkedRole.
###############################################################################

resource "helm_release" "aws_lbc" {
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = var.chart_version
  namespace  = var.namespace

  values = [yamlencode({
    clusterName = var.cluster_name
    region      = var.region
    vpcId       = var.vpc_id

    # Use IRSA: create the SA here and annotate it with the role from modules/iam.
    serviceAccount = {
      create = true
      name   = var.service_account_name
      annotations = {
        "eks.amazonaws.com/role-arn" = var.irsa_role_arn
      }
    }

    # Run on system nodes (managed node group hosts kube-system — Component 4).
    nodeSelector = {
      "node-role" = "system"
    }
    tolerations = [
      {
        key      = "CriticalAddonsOnly"
        operator = "Equal"
        value    = "true"
        effect   = "NoSchedule"
      }
    ]
  })]
}
