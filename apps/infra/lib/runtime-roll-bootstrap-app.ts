import * as cdk from "aws-cdk-lib";
import { RuntimeRollBootstrapStack } from "./runtime-roll-bootstrap";

const app = new cdk.App();
new RuntimeRollBootstrapStack(app, "lightbulb-runtime-roll-bootstrap", {
  env: { account: "877352799272", region: "us-east-1" },
});
