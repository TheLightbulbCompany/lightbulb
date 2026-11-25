import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import { Construct } from "constructs";
import { ROOT_DOMAIN } from "../domains";

export interface DnsStackProps extends cdk.StackProps {
  environment: string;
  /** Product root domain (see lib/domains.ts). Defaults to ROOT_DOMAIN. */
  rootDomain?: string;
}

/**
 * DNS + TLS for the product domain: the hosted zone lookup and one wildcard
 * certificate. The certificate ARN is a cross-stack Export the api stack
 * imports, so never change its logical id `RootWildcardCert`: that replaces the
 * certificate, and CloudFormation refuses to change an export in use.
 */
export class DnsStack extends cdk.Stack {
  public readonly certificate: acm.ICertificate;
  /** Zone for the product domain (where the api record is written). */
  public readonly hostedZone: route53.IHostedZone;

  constructor(scope: Construct, id: string, props: DnsStackProps) {
    super(scope, id, props);

    const rootDomain = props.rootDomain ?? ROOT_DOMAIN;
    this.hostedZone = route53.HostedZone.fromLookup(this, "ProductHostedZone", {
      domainName: rootDomain,
    });
    this.certificate = new acm.Certificate(this, "RootWildcardCert", {
      domainName: `*.${rootDomain}`,
      subjectAlternativeNames: [rootDomain],
      validation: acm.CertificateValidation.fromDns(this.hostedZone),
    });
  }
}
