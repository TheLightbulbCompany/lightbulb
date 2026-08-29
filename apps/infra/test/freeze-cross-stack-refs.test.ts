import { describe, expect, it } from "vitest";
import * as cdk from "aws-cdk-lib";
import * as sqs from "aws-cdk-lib/aws-sqs";
import { Template } from "aws-cdk-lib/assertions";

import { freezeCrossStackRefs } from "../lib/freeze-cross-stack-refs";
import { DEPLOYED_EXPORT_VALUES } from "../lib/rename-export-values";

/**
 * Guards the transitional shim that breaks cross-stack imports so the
 * isol8-* -> lightbulb-* stack rename can be done with CreateStackRefactor.
 * Delete alongside the shim once the rename has shipped.
 */
class Producer extends cdk.Stack {
  public readonly queue: sqs.Queue;
  constructor(scope: cdk.Stage, id: string) {
    super(scope, id, { stackName: id });
    this.queue = new sqs.Queue(this, "Q");
  }
}
class Consumer extends cdk.Stack {
  constructor(scope: cdk.Stage, id: string, producer: Producer) {
    super(scope, id, { stackName: id });
    new sqs.Queue(this, "Dlq", { queueName: cdk.Fn.select(5, cdk.Fn.split(":", producer.queue.queueArn)) });
  }
}

function build(freeze: boolean, snapshot: Record<string, string> = {}) {
  const app = new cdk.App();
  const stage = new cdk.Stage(app, "test");
  const producer = new Producer(stage, "producer");
  const consumer = new Consumer(stage, "consumer", producer);
  if (freeze) {
    // Point the shim's snapshot at whatever this test wants frozen.
    DEPLOYED_EXPORT_VALUES.unit = snapshot;
    freezeCrossStackRefs(stage, "unit");
  }
  app.synth();
  return { producer: Template.fromStack(producer), consumer: Template.fromStack(consumer) };
}

function exportNameOf(producer: Template): string {
  const outputs = producer.findOutputs("*");
  const [only] = Object.values(outputs);
  return (only as { Export: { Name: string } }).Export.Name;
}

describe("freezeCrossStackRefs", () => {
  it("leaves the reference alone when the export has no snapshot entry", () => {
    const { consumer } = build(true, {});
    expect(JSON.stringify(consumer.toJSON())).toContain("Fn::ImportValue");
  });

  it("replaces the import with the deployed literal", () => {
    const name = exportNameOf(build(false).producer);
    const { consumer } = build(true, { [name]: "arn:aws:sqs:us-east-1:1:frozen" });
    const json = JSON.stringify(consumer.toJSON());
    expect(json).not.toContain("Fn::ImportValue");
    expect(json).toContain("arn:aws:sqs:us-east-1:1:frozen");
  });

  it("keeps publishing the export, so the producer never deletes one still in use", () => {
    const name = exportNameOf(build(false).producer);
    const { producer } = build(true, { [name]: "arn:aws:sqs:us-east-1:1:frozen" });
    expect(exportNameOf(producer)).toEqual(name);
  });

  it("emits no Ref to a logical id the consuming stack does not define", () => {
    const name = exportNameOf(build(false).producer);
    const { consumer } = build(true, { [name]: "arn:aws:sqs:us-east-1:1:frozen" });
    const template = consumer.toJSON();
    const known = new Set([...Object.keys(template.Resources ?? {}), ...Object.keys(template.Parameters ?? {})]);
    const dangling: string[] = [];
    const walk = (node: unknown): void => {
      if (Array.isArray(node)) return node.forEach(walk);
      if (node && typeof node === "object") {
        for (const [k, v] of Object.entries(node)) {
          if (k === "Ref" && typeof v === "string" && !v.startsWith("AWS::") && !known.has(v)) dangling.push(v);
          walk(v);
        }
      }
    };
    walk(template.Resources);
    expect(dangling).toEqual([]);
  });
});
