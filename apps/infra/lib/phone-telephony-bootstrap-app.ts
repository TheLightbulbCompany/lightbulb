import * as cdk from "aws-cdk-lib";
import { PhoneTelephonyBootstrapStack } from "./phone-telephony-bootstrap";

const app = new cdk.App();
new PhoneTelephonyBootstrapStack(app, "lightbulb-phone-telephony-bootstrap", {
  env: { account: "877352799272", region: "us-east-1" },
});
app.synth();
