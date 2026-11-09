import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { PhoneTelephonyBootstrapStack } from "../lib/phone-telephony-bootstrap";

const template = Template.fromStack(new PhoneTelephonyBootstrapStack(new cdk.App(), "PhoneBootstrap", {
  env: { account: "877352799272", region: "us-east-1" },
}));
const policy: any = Object.values(template.findResources("AWS::IAM::Policy"))[0].Properties;
const statements = policy.PolicyDocument.Statement;
const arnText = (value: any): string => typeof value === "string" ? value : value["Fn::Join"][1]
  .map((part: any) => typeof part === "string" ? part : part.Ref === "AWS::Partition" ? "aws" : "UNEXPECTED_TOKEN").join("");

test("creates a dedicated main-only role without modifying the existing deployer or OIDC provider", () => {
  template.resourceCountIs("AWS::IAM::Role", 1);
  template.resourceCountIs("AWS::IAM::OIDCProvider", 0);
  const roles: any[] = Object.values(template.findResources("AWS::IAM::Role"));
  expect(roles[0].Properties.RoleName).toBe("lightbulb-phone-telephony");
  const trust = roles[0].Properties.AssumeRolePolicyDocument.Statement[0];
  expect(trust.Action).toBe("sts:AssumeRoleWithWebIdentity");
  expect(trust.Condition.StringEquals).toEqual({
    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
    "token.actions.githubusercontent.com:sub": "repo:TheLightbulbCompany@253404738/lightbulb@1182791203:ref:refs/heads/main",
  });
  expect(Object.keys(template.toJSON().Resources)).toHaveLength(2);
  expect(JSON.stringify(policy.Roles)).not.toContain("lightbulb-github-actions");
  expect(statements.flatMap((s: any) => s.Action)).not.toContain("iam:*");
});

test("reads exactly the five existing phone credentials and writes only the config secret", () => {
  const read = statements.find((s: any) => s.Sid === "ReadExistingPhoneCredentials");
  expect(read.Action).toEqual(["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]);
  expect(read.Resource).toHaveLength(5);
  for (const arn of read.Resource) expect(arnText(arn)).toMatch(/:secret:lightbulb\/(dev\/twilio_(account_sid|api_key_sid|api_key_secret)|prod\/livekit_api_(key|secret))-\?{6}$/);
  for (const s of statements.filter((s: any) => [s.Action].flat().some((a: string) => /secretsmanager:(Create|Put)/.test(a)))) {
    expect(arnText(s.Resource)).toMatch(/:secret:lightbulb\/prod\/voice_telephony-\?{6}$/);
  }
  expect(statements.find((s: any) => s.Sid === "CreateOnlyPhoneConfiguration").Condition.StringEquals)
    .toEqual({ "secretsmanager:Name": "lightbulb/prod/voice_telephony" });
});

test("KMS access requires Secrets Manager and the exact permitted encryption contexts", () => {
  for (const s of statements.filter((s: any) => s.Sid.includes("ThroughSecretsManager"))) {
    expect(s.Condition.StringEquals["kms:ViaService"]).toBe("secretsmanager.us-east-1.amazonaws.com");
    expect(s.Condition.StringLike["kms:EncryptionContext:SecretARN"]).toBeTruthy();
    expect([s.Resource].flat().every((r: string) => /^arn:aws:kms:us-east-1:877352799272:key\//.test(r))).toBe(true);
    expect(s.Resource).not.toBe("*");
  }
});
