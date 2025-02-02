# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
from typing import List

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    StackProps
)
from aws_cdk.aws_ec2 import (
    IVpc,
    SubnetSelection,
    SubnetType
)
from aws_cdk.aws_elasticloadbalancingv2 import (
    ApplicationProtocol
)
from aws_cdk.aws_route53 import (
    IPrivateHostedZone
)
from aws_rfdk import (
    DistinguishedName,
    SessionManagerHelper,
    X509CertificatePem
)
from aws_rfdk.deadline import (
    AwsCustomerAgreementAndIpLicenseAcceptance,
    RenderQueue,
    RenderQueueHostNameProps,
    RenderQueueTrafficEncryptionProps,
    RenderQueueExternalTLSProps,
    Repository,
    RepositoryRemovalPolicies,
    ThinkboxDockerImages,
    VersionQuery
)
from constructs import (
    Construct
)

@dataclass
class ServiceTierProps(StackProps):
    """
    Properties for ServiceTier
    """
    # The VPC to deploy service tier resources into.
    vpc: IVpc
    # Whether the AWS Customer Agreement and AWS Intellectual Property License are agreed to.
    user_aws_customer_agreement_and_ip_license_acceptance: AwsCustomerAgreementAndIpLicenseAcceptance
    # The availability zones that components in this stack will be deployed into. These should all be in the same
    # region and only be standard availability zones, as some constucts use services that aren't available in
    # local zones yet.
    availability_zones: List[str]
    # Version of Deadline to use
    deadline_version: str
    # Internal DNS zone for the VPC
    dns_zone: IPrivateHostedZone
    # Our self-signed root CA certificate for the internal endpoints in the farm.
    root_ca: X509CertificatePem


class ServiceTier(Stack):
    """
    The service tier contains all "business-logic" constructs
    (e.g. Repository, Render Queue, etc.)
    """

    def __init__(self, scope: Construct, stack_id: str, *, props: ServiceTierProps, **kwargs):
        """
        Initialize a new instance of ServiceTier
        :param scope: The scope of this construct.
        :param stack_id: The ID of this construct.
        :param props: The properties for this construct.
        :param kwargs: Any kwargs that need to be passed on to the parent class.
        """
        super().__init__(scope, stack_id, **kwargs)

        self.version = VersionQuery(
            self,
            'Version',
            version=props.deadline_version
        )

        # We are excluding the local zones from the Repository. This construct will create an
        # EFS filesystem and DocDB cluster, both of which aren't available in any local zones at this time.
        repository_subnets = SubnetSelection(
            availability_zones=props.availability_zones,
            subnet_type=SubnetType.PRIVATE_WITH_NAT
        )
        repository = Repository(
            self,
            'Repository',
            vpc=props.vpc,
            repository_installation_timeout=Duration.minutes(20),
            removal_policy=RepositoryRemovalPolicies(
                database=RemovalPolicy.DESTROY,
                filesystem=RemovalPolicy.DESTROY,
            ),
            version=self.version,
            vpc_subnets=repository_subnets
        )

        images = ThinkboxDockerImages(
            self,
            'Images',
            version=self.version,
            user_aws_customer_agreement_and_ip_license_acceptance=props.user_aws_customer_agreement_and_ip_license_acceptance
        )

        server_cert = X509CertificatePem(
            self,
            'RQCert',
            subject=DistinguishedName(
                cn=f'renderqueue.{props.dns_zone.zone_name}',
                o='RFDK-Sample',
                ou='RenderQueueExternal'
            ),
            signing_certificate=props.root_ca
        )

        # The render queue is also put only in the standard availability zones. The service itself
        # is run in a single zone, while the load balancer that sits in front of it can be provided
        # all the standard zones we're using.
        render_queue_subnets = SubnetSelection(
            availability_zones=[props.availability_zones[0]],
            subnet_type=SubnetType.PRIVATE_WITH_NAT
        )
        render_queue_alb_subnets = SubnetSelection(
            availability_zones=props.availability_zones,
            subnet_type=SubnetType.PRIVATE_WITH_NAT,
            one_per_az=True,
        )
        self.render_queue = RenderQueue(
            self,
            'RenderQueue',
            vpc=props.vpc,
            images=images,
            repository=repository,
            hostname=RenderQueueHostNameProps(
                hostname='renderqueue',
                zone=props.dns_zone
            ),
            traffic_encryption=RenderQueueTrafficEncryptionProps(
                external_tls=RenderQueueExternalTLSProps(
                    rfdk_certificate=server_cert
                ),
                internal_protocol=ApplicationProtocol.HTTPS
            ),
            version=self.version,
            vpc_subnets=render_queue_subnets,
            vpc_subnets_alb=render_queue_alb_subnets,
            deletion_protection=False
        )
        SessionManagerHelper.grant_permissions_to(self.render_queue.asg)
