import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { describe, expect, it } from "vitest";
import { DnsStack } from "../lib/stacks/dns-stack";

const env = { account: "877352799272", region: "us-east-1" };

// The certificate ARN is a cross-stack Export. A new logical id replaces the
// certificate in place, and the deploy rolls back.
describe("DnsStack certificate", () => {
  it("keeps the logical id RootWildcardCert", () => {
    const app = new cdk.App();
    const stack = new DnsStack(app, "dns", { env, environment: "dev", rootDomain: "thelightbulb.company" });
    const found = Template.fromStack(stack).findResources("AWS::CertificateManager::Certificate");
    expect(Object.keys(found)).toHaveLength(1);
    expect(Object.keys(found)[0]).toMatch(/^RootWildcardCert/);
  });
});
