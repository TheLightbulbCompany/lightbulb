import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import { Construct } from "constructs";
import { ROOT_DOMAIN } from "../domains";

export interface DnsStackProps extends cdk.StackProps {
  environment: string;
  /** Product root domain (see lib/domains.ts). Defaults to ROOT_DOMAIN. */
  rootDomain?: string;
  /** The domain being moved away from. Empty or equal to rootDomain = no move in flight. */
  legacyRootDomain?: string;
}

/**
 * DNS + TLS for the product domain.
 *
 * The certificate ARN is a cross-stack CloudFormation Export (network + api
 * import it), and CloudFormation refuses to change an export that is in use.
 * So a domain move never edits the certificate in place: the old one stays
 * under its original logical id, pinned with `exportValue`, while the new one
 * is created beside it and the consumers repoint in the same deploy. Clearing
 * LEGACY_ROOT_DOMAIN later drops the old certificate once nothing imports it.
 * (Editing in place rolled the whole stage back on 2026-08-27.)
 */
export class DnsStack extends cdk.Stack {
  /** The certificate consumers should use — the product domain's. */
  public readonly certificate: acm.ICertificate;
  /** Zone for the product domain (where api/ws/bv records are written). */
  public readonly hostedZone: route53.IHostedZone;

  constructor(scope: Construct, id: string, props: DnsStackProps) {
    super(scope, id, props);

    const rootDomain = props.rootDomain ?? ROOT_DOMAIN;
    const legacy = props.legacyRootDomain ?? "";
    const moving = legacy !== "" && legacy !== rootDomain;

    if (moving) {
      const legacyZone = route53.HostedZone.fromLookup(this, "LegacyHostedZone", {
        domainName: legacy,
      });
      const legacyCert = new acm.Certificate(this, "ProductWildcardCert", {
        domainName: `*.${legacy}`,
        subjectAlternativeNames: [legacy],
        validation: acm.CertificateValidation.fromDns(legacyZone),
      });
      this.exportValue(legacyCert.certificateArn);
    }

    this.hostedZone = route53.HostedZone.fromLookup(this, "ProductHostedZone", {
      domainName: rootDomain,
    });
    // "ProductWildcardCert" is the id the certificate has had since before any
    // move; it keeps that id until the root actually changes.
    const certId = legacy === rootDomain ? "ProductWildcardCert" : "RootWildcardCert";
    this.certificate = new acm.Certificate(this, certId, {
      domainName: `*.${rootDomain}`,
      subjectAlternativeNames: [rootDomain],
      validation: acm.CertificateValidation.fromDns(this.hostedZone),
    });
  }
}
