import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import { Construct } from "constructs";
import { LEGACY_ROOT_DOMAIN, ROOT_DOMAIN } from "../domains";

export interface DnsStackProps extends cdk.StackProps {
  environment: string;
  /** Product root domain (see lib/domains.ts). Defaults to ROOT_DOMAIN. */
  rootDomain?: string;
}

/**
 * DNS + TLS for the product domain.
 *
 * ## Why there are two certificates during the migration
 *
 * `WildcardCert` covers the LEGACY domain and its ARN is a cross-stack
 * CloudFormation **Export**, imported by `isol8-{env}-network` (the ALB HTTPS
 * listener) and `isol8-{env}-api`. CloudFormation refuses to change or delete
 * an export while anything imports it:
 *
 *     Cannot update export isol8-dev-dns:ExportsOutputRefWildcardCert...
 *     as it is in use by isol8-dev-api and isol8-dev-network.
 *
 * So flipping `rootDomain` by editing `WildcardCert` in place cannot work — it
 * replaces the certificate, which changes the exported ARN, and the whole stage
 * rolls back. (Observed exactly this on 2026-08-27.)
 *
 * The migration shape instead: leave `WildcardCert` **untouched** so its export
 * keeps resolving, add a SECOND certificate under a new logical id for the new
 * domain, and repoint the consumers at it in the same deploy. The old export
 * then simply has no importers, and the construct can be deleted later in its
 * own change.
 *
 * When `rootDomain` is still the legacy domain (prod today) no second
 * certificate is created and this is a no-op.
 */
export class DnsStack extends cdk.Stack {
  /** The certificate consumers should use — the product domain's. */
  public readonly certificate: acm.ICertificate;
  /** Zone for the product domain (where api/ws/bv records are written). */
  public readonly hostedZone: route53.IHostedZone;
  /** Legacy zone, kept while the old domain still serves. */
  public readonly legacyHostedZone: route53.IHostedZone;

  constructor(scope: Construct, id: string, props: DnsStackProps) {
    super(scope, id, props);

    const rootDomain = props.rootDomain ?? ROOT_DOMAIN;
    const migrating = rootDomain !== LEGACY_ROOT_DOMAIN;

    this.legacyHostedZone = route53.HostedZone.fromLookup(this, "HostedZone", {
      domainName: LEGACY_ROOT_DOMAIN,
    });

    // Logical id and props frozen: any drift here replaces an in-use export.
    const legacyCert = new acm.Certificate(this, "WildcardCert", {
      domainName: `*.${LEGACY_ROOT_DOMAIN}`,
      subjectAlternativeNames: [LEGACY_ROOT_DOMAIN],
      validation: acm.CertificateValidation.fromDns(this.legacyHostedZone),
    });

    if (!migrating) {
      this.hostedZone = this.legacyHostedZone;
      this.certificate = legacyCert;
      return;
    }

    // Repointing the consumers leaves nothing importing the legacy export, and
    // CDK only emits an export that something imports - so it would vanish from
    // the template and CloudFormation would try to DELETE it while the
    // not-yet-updated network/api stacks still import it. Same deadlock, one
    // step later. exportValue() pins it in place for this deploy; drop this line
    // (and the cert) in a LATER change, once no stack imports it.
    this.exportValue(legacyCert.certificateArn);

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
