import * as cdk from "aws-cdk-lib";

import { DEPLOYED_EXPORT_VALUES } from "./rename-export-values";

/**
 * TRANSITIONAL — step 2 of 3 in renaming `isol8-{env}-*` -> `lightbulb-{env}-*`.
 * Delete this file, `rename-export-values.ts` and the call in isol8-stage.ts
 * once step 3 has shipped to both environments.
 *
 * THE PROBLEM
 * -----------
 * CloudFormation has no stack rename. The supported move is
 * `CreateStackRefactor`, which is strictly limited: "a refactor operation
 * cannot add, remove or update resources -- only resource moves and renames
 * are allowed". Two separate things break that rule:
 *
 *   1. CDK derives cross-stack export names from the PRODUCING stack's name
 *      (`generateExportName` in aws-cdk-lib/core/lib/stack.js), so renaming a
 *      stack rewrites the `Fn::ImportValue` STRING baked into every consumer's
 *      resource properties -- a property UPDATE, which the refactor rejects.
 *   2. Even for a hand-written deploy, CloudFormation refuses to delete or
 *      rename an export while another stack imports it. Verified the hard way:
 *      refactoring `isol8-dev-dns` alone rolled back with "Cannot delete export
 *      isol8-dev-dns:ExportsOutputRefProductWildcardCert... as it is in use by
 *      isol8-dev-api and isol8-dev-network". Putting every stack in ONE
 *      refactor does not help -- the importer's property still changes.
 *
 * THE FIX
 * -------
 * Break the imports first, in an ordinary deploy, where property updates ARE
 * allowed. This freezes every cross-stack reference to the value already
 * deployed, so consumers stop importing and every export goes unused. The
 * refactor is then a pure move, and step 3 restores real exports under the new
 * stack names -- resolving to the same values, so nothing churns.
 *
 * The producing stack keeps publishing its export (we call `super` and only
 * change what the CONSUMER embeds). Dropping it here instead would delete an
 * export in the same deploy that stops importing it, and stacks deploy one at
 * a time: the producer would go first and fail on an export still in use.
 *
 * Values come from `rename-export-values.ts`, snapshotted from
 * `aws cloudformation list-exports` -- the deployed truth, not a guess. A
 * reference with no snapshot entry (anything added since) falls back to the
 * normal import, which is correct because its export is created in this same
 * deploy.
 */
export function freezeCrossStackRefs(stage: cdk.Stage, environment: string): void {
  const snapshot: Record<string, string> = DEPLOYED_EXPORT_VALUES[environment] ?? {};

  for (const child of stage.node.children) {
    if (!cdk.Stack.isStack(child)) {
      continue;
    }
    const stack = child;
    const inherited = stack.exportValue.bind(stack);

    stack.exportValue = (value: unknown, options: cdk.ExportValueOptions = {}): string => {
      // Let CDK do all of its normal work first: allocate the `Exports` scope,
      // dedupe, create the CfnOutput, and — load-bearing — keep the Reference
      // that derives `consumer.addDependency(producer)`, which is what
      // generates deploy.yml's `needs:` edges.
      const token = inherited(value, options);
      const name = stack.resolve(token)?.["Fn::ImportValue"];
      if (typeof name !== "string") {
        return token;
      }
      const frozen = snapshot[name];
      if (frozen === undefined) {
        return token;
      }
      // MUST stay an unresolved Token. `createImportValue` in
      // aws-cdk-lib/core/lib/private/refs.js only registers the substitution
      // for a token; hand it a plain string and it silently keeps the original
      // CfnReference, which then resolves in the CONSUMING stack as a local
      // `Ref` to a logical id that only exists in the PRODUCING one. That
      // yields a template CloudFormation rejects -- 471 such dangling refs
      // before this was a Lazy.
      return cdk.Lazy.uncachedString({ produce: () => frozen });
    };
  }
}
