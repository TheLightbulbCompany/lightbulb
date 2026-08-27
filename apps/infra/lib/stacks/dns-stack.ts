import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import { Construct } from "constructs";
import { ROOT_DOMAIN } from "../domains";

export interface DnsStackProps extends cdk.StackProps {
  environment: string;
  /**
   * Product root domain (see lib/domains.ts). Defaults to ROOT_DOMAIN.
   *
   * Changing this REPLACES `WildcardCert`, whose ARN is a cross-stack CFN
   * Export consumed by the network + api stacks — CloudFormation refuses to
   * update an export that is in use, so a value change has to land as a NEW
   * certificate construct with the consumers repointed in the same PR.
   */
  rootDomain?: string;
}

export class DnsStack extends cdk.Stack {
  public readonly certificate: acm.ICertificate;
  public readonly hostedZone: route53.IHostedZone;

  constructor(scope: Construct, id: string, props: DnsStackProps) {
    super(scope, id, props);

    const rootDomain = props.rootDomain ?? ROOT_DOMAIN;

    this.hostedZone = route53.HostedZone.fromLookup(this, "HostedZone", {
      domainName: rootDomain,
    });

    this.certificate = new acm.Certificate(this, "WildcardCert", {
      domainName: `*.${rootDomain}`,
      subjectAlternativeNames: [rootDomain],
      validation: acm.CertificateValidation.fromDns(this.hostedZone),
    });
  }
}
