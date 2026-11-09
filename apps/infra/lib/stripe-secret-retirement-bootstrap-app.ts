import * as cdk from "aws-cdk-lib";
import { StripeSecretRetirementBootstrapStack } from "./stripe-secret-retirement-bootstrap";

const app = new cdk.App();
new StripeSecretRetirementBootstrapStack(app, "lightbulb-stripe-secret-retirement-bootstrap", {
  env: { account: "877352799272", region: "us-east-1" },
});
app.synth();
