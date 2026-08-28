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
 * DNS + TLS for the product domain.
 *
 * ## One certificate again — the migration finished
 *
 * There were briefly two. `WildcardCert` covered the LEGACY domain and its ARN
 * was a cross-stack CloudFormation **Export** imported by `isol8-{env}-network`
 * (the ALB HTTPS listener) and `isol8-{env}-api`. CloudFormation refuses to
 * change or delete an export while anything imports it:
 *
 *     Cannot update export isol8-dev-dns:ExportsOutputRefWildcardCert...
 *     as it is in use by isol8-dev-api and isol8-dev-network.
 *
 * so it could not be edited in place — editing REPLACES the certificate, which
 * changes the exported ARN, and the whole stage rolls back. (Observed exactly
 * that on 2026-08-27.) The shape used instead: leave the legacy cert untouched,
 * add a second one under a new logical id, repoint consumers in the same
 * deploy, then delete the legacy construct in a later change once its export
 * had no importers.
 *
 * This is that later change. Verified before removal, from live AWS rather than
 * from the code: `cloudformation list-imports` reports NO importers for
 * `ExportsOutputRefWildcardCert…` in either env, `ProductWildcardCert` is
 * imported by api + network, and both `*.isol8.co` certificates are
 * `InUse=False` in ACM.
 *
 * NOTE the isol8.co hosted ZONE deliberately survives this: GooseTown still
 * serves 9 records out of it. Retiring our certificate is not retiring the zone.
 */
export class DnsStack extends cdk.Stack {
  /** The certificate consumers should use — the product domain's. */
  public readonly certificate: acm.ICertificate;
  /** Zone for the product domain (where api/ws/bv records are written). */
  public readonly hostedZone: route53.IHostedZone;

  constructor(scope: Construct, id: string, props: DnsStackProps) {
    super(scope, id, props);

    const rootDomain = props.rootDomain ?? ROOT_DOMAIN;

    this.hostedZone = route53.HostedZone.fromLookup(this, "ProductHostedZone", {
      domainName: rootDomain,
    });
    this.certificate = new acm.Certificate(this, "ProductWildcardCert", {
      domainName: `*.${rootDomain}`,
      subjectAlternativeNames: [rootDomain],
      validation: acm.CertificateValidation.fromDns(this.hostedZone),
    });
  }
}
