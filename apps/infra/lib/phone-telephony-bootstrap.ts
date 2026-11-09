import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";

/** Standalone CI bootstrap: no dependency on the platform stack that consumes the resulting secret. */
export class PhoneTelephonyBootstrapStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);
    const secretArn = (name: string) => this.formatArn({
      service: "secretsmanager", resource: "secret", resourceName: `${name}-??????`,
      arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
    });
    const inputs = [
      "lightbulb/dev/twilio_account_sid",
      "lightbulb/dev/twilio_api_key_sid",
      "lightbulb/dev/twilio_api_key_secret",
      "lightbulb/prod/livekit_api_key",
      "lightbulb/prod/livekit_api_secret",
    ].map(secretArn);
    const output = secretArn("lightbulb/prod/voice_telephony");
    const phoneRole = new iam.Role(this, "PhoneProvisioningRole", {
      roleName: "lightbulb-phone-telephony",
      description: "Main-only phone-telephony workflow; no platform deployment permissions",
      maxSessionDuration: cdk.Duration.hours(1),
      assumedBy: new iam.FederatedPrincipal(
        "arn:aws:iam::877352799272:oidc-provider/token.actions.githubusercontent.com",
        { StringEquals: {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub": "repo:TheLightbulbCompany@253404738/lightbulb@1182791203:ref:refs/heads/main",
        } },
        "sts:AssumeRoleWithWebIdentity",
      ),
    });
    const policy = new iam.Policy(this, "PhoneProvisioningSecrets", {
      policyName: "lightbulb-phone-telephony-provisioning",
      statements: [
        new iam.PolicyStatement({
          sid: "ReadExistingPhoneCredentials",
          actions: ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
          resources: inputs,
        }),
        new iam.PolicyStatement({
          sid: "ReadAndWritePhoneConfiguration",
          actions: ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret", "secretsmanager:PutSecretValue"],
          resources: [output],
        }),
        new iam.PolicyStatement({
          sid: "CreateOnlyPhoneConfiguration",
          actions: ["secretsmanager:CreateSecret"],
          resources: [output],
          conditions: { StringEquals: { "secretsmanager:Name": "lightbulb/prod/voice_telephony" } },
        }),
        new iam.PolicyStatement({
          sid: "DecryptPhoneCredentialsOnlyThroughSecretsManager",
          actions: ["kms:Decrypt"],
          // Existing prod AuthStack CMK and the account's aws/secretsmanager key holding the Twilio credentials.
          resources: [
            "arn:aws:kms:us-east-1:877352799272:key/9b6e55b3-aba8-4c2e-accd-f7744f966caa",
            "arn:aws:kms:us-east-1:877352799272:key/5b1e9e03-7d7d-46ca-bbbe-f31802b0783a",
          ],
          conditions: {
            StringEquals: { "kms:ViaService": "secretsmanager.us-east-1.amazonaws.com" },
            StringLike: { "kms:EncryptionContext:SecretARN": [...inputs, output] },
          },
        }),
        new iam.PolicyStatement({
          sid: "EncryptOnlyPhoneConfigurationThroughSecretsManager",
          actions: ["kms:GenerateDataKey"],
          resources: ["arn:aws:kms:us-east-1:877352799272:key/9b6e55b3-aba8-4c2e-accd-f7744f966caa"],
          conditions: {
            StringEquals: { "kms:ViaService": "secretsmanager.us-east-1.amazonaws.com" },
            StringLike: { "kms:EncryptionContext:SecretARN": output },
          },
        }),
      ],
    });
    policy.attachToRole(phoneRole);
  }
}
