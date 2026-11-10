import * as cdk from "aws-cdk-lib";
import { LegacyRuntimeRetirementBootstrapStack } from "./legacy-runtime-retirement-bootstrap";

const app = new cdk.App();
new LegacyRuntimeRetirementBootstrapStack(app, "lightbulb-legacy-runtime-retirement-bootstrap", {
  env: { account: "877352799272", region: "us-east-1" },
});
app.synth();
