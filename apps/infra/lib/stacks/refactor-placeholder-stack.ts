import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";

/**
 * TRANSITIONAL — step 1 of 3 in renaming the stacks `isol8-{env}-*` ->
 * `lightbulb-{env}-*`.
 *
 * WHY THIS EXISTS
 * ---------------
 * CloudFormation has no stack rename, so the destination of a rename is, to
 * CloudFormation, a NEW stack. The supported way to move resources without
 * recreating them is `cdk refactor` — but the CLI refuses when the destination
 * does not exist yet:
 *
 *     The following stack is new: lightbulb-dev-agent-mail-outbound
 *     Creation of new stacks is not yet supported by the refactor command.
 *     Please deploy any new stacks separately before refactoring your stacks.
 *
 * Verified against CDK CLI 2.1139.0 (the latest at time of writing) with the
 * bootstrap stack upgraded to v32, so it is a gap in the preview feature and
 * not a stale CLI or a missing bootstrap permission.
 *
 * This stack is that "deploy any new stacks separately" step: it claims each
 * destination stack NAME with the cheapest possible resource, so a later
 * `cdk refactor` has somewhere to move the real resources into.
 *
 * WHY A REAL RESOURCE AND NOT AN EMPTY STACK
 * ------------------------------------------
 * CloudFormation does not support empty stacks. An SNS topic is used rather
 * than the `AWS::CloudFormation::WaitCondition` the docs suggest, because
 * WaitCondition is itself on the refactor unsupported-types list and would
 * become a second problem later.
 *
 * THE PLACEHOLDER MUST SURVIVE STEP 2
 * -----------------------------------
 * `cdk refactor` rejects any operation that also adds, deletes or modifies a
 * resource. So the placeholder cannot be dropped in the same change that moves
 * the real resources in — it is removed in step 3, as an ordinary deploy.
 *
 * Delete this file, and its call site in isol8-stage.ts, once step 3 has
 * shipped to both environments.
 */
export class RefactorPlaceholderStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: cdk.StackProps) {
    super(scope, id, props);

    // STEP 1.5 — the topic is REMOVED, leaving the stack on CDKMetadata alone.
    //
    // The topic did its job: it made the destination stacks creatable (CFN
    // rejects a template with no resources). But it then BLOCKED the refactor.
    // `cdk refactor` matches resources by their `aws:cdk:path`, and the topic's
    // path is `<env>/lightbulb-<env>-<x>/RefactorPlaceholder/Resource` as
    // deployed versus `<env>/isol8-<env>-<x>/RefactorPlaceholder/Resource` once
    // it is re-attached to the renamed real stack — so refactor saw a delete
    // plus an add, and it refuses any operation containing either. An
    // --override-file cannot express it: overrides key on
    // `<stack>.<logicalId>`, which is IDENTICAL on both sides here.
    //
    // Removing it makes the destination stacks empty from refactor's point of
    // view, because refactor ignores CDKMetadata entirely (it never appears in
    // a refactor diff). That is what turns step 2 into a pure move.
  }
}
