import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { describe, expect, it } from "vitest";
import { DnsStack } from "../lib/stacks/dns-stack";

const env = { account: "877352799272", region: "us-east-1" };

function certs(rootDomain: string, legacyRootDomain?: string) {
  const app = new cdk.App();
  const stack = new DnsStack(app, "dns", { env, environment: "dev", rootDomain, legacyRootDomain });
  const found = Template.fromStack(stack).findResources("AWS::CertificateManager::Certificate");
  return Object.fromEntries(
    Object.entries(found).map(([id, r]) => [(r as { Properties: { DomainName: string } }).Properties.DomainName, id]),
  );
}

// The certificate ARN is a cross-stack Export. Replacing it in place rolls the
// stage back, so a move must add the new certificate beside the old one.
describe("DnsStack certificate across a domain move", () => {
  it("keeps the original logical id when nothing has moved", () => {
    const byDomain = certs("flickmeon.ai", "flickmeon.ai");
    expect(Object.keys(byDomain)).toEqual(["*.flickmeon.ai"]);
    expect(byDomain["*.flickmeon.ai"]).toMatch(/^ProductWildcardCert/);
  });

  it("adds the new certificate beside the old one during a move", () => {
    const byDomain = certs("thelightbulb.company", "flickmeon.ai");
    expect(byDomain["*.flickmeon.ai"]).toMatch(/^ProductWildcardCert/);
    expect(byDomain["*.thelightbulb.company"]).toMatch(/^RootWildcardCert/);
  });

  it("keeps exporting the old certificate during a move, so importers do not block the deploy", () => {
    const app = new cdk.App();
    const stack = new DnsStack(app, "dns", {
      env,
      environment: "dev",
      rootDomain: "thelightbulb.company",
      legacyRootDomain: "flickmeon.ai",
    });
    const outputs = Object.keys(Template.fromStack(stack).toJSON().Outputs ?? {});
    expect(outputs.some((o) => o.startsWith("ExportsOutputRefProductWildcardCert"))).toBe(true);
  });

  it("keeps only the new certificate once the legacy domain is cleared", () => {
    const byDomain = certs("thelightbulb.company", "");
    expect(Object.keys(byDomain)).toEqual(["*.thelightbulb.company"]);
    expect(byDomain["*.thelightbulb.company"]).toMatch(/^RootWildcardCert/);
  });
});
