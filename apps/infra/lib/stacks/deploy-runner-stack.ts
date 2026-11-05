import * as fs from "fs";
import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as codebuild from "aws-cdk-lib/aws-codebuild";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";

export interface DeployRunnerStackProps extends cdk.StackProps {
  envName: string;
}

/**
 * The dev deploy runner: a CodeBuild project that, on every push to main, does
 * what the dev stage of the generated .github/workflows/deploy.yml does --
 * synth, publish assets, deploy the dev stacks (this one included), then the
 * two Vercel previews -- by running apps/infra/codebuild/deploy-dev.sh. Stood
 * up because the self-hosted deploy runner is offline and there is no Actions
 * budget (2026-11-05). Prod promotion stays on GitHub Actions. Dev-only: the
 * stage instantiates it for dev, and role-policy.json names dev resources.
 *
 * What the build needs that is not in the repo, the lead creates by hand
 * (apps/infra/codebuild/README.md): the GitHub credential CodeBuild clones and
 * creates the webhook with, and the Secrets Manager secret
 * `lightbulb/dev/deploy-runner` holding the synth and Vercel values that
 * deploy.yml reads from GitHub secrets.
 *
 * The buildspec and script are read from the pushed commit
 * (BuildSpec.fromSourceFilename), so a change to them ships with the push that
 * carries it. The role policy is apps/infra/codebuild/role-policy.json
 * verbatim, so what the runner may do is reviewed as JSON and applied here.
 */
export class DeployRunnerStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: DeployRunnerStackProps) {
    super(scope, id, props);
    const envName = props.envName;
    const codebuildDir = path.join(__dirname, "..", "..", "codebuild");

    const project = new codebuild.Project(this, "Project", {
      projectName: `lightbulb-${envName}-deploy`,
      description: `Deploys the ${envName} stage on every push to main (apps/infra/codebuild/)`,
      source: codebuild.Source.gitHub({
        owner: "TheLightbulbCompany",
        repo: "lightbulb",
        cloneDepth: 1,
        // Commit status on GitHub (lightbulb-dev-deploy: pending/success/failure).
        reportBuildStatus: true,
        webhook: true,
        webhookFilters: [
          codebuild.FilterGroup.inEventOf(codebuild.EventAction.PUSH).andHeadRefIs("^refs/heads/main$"),
        ],
      }),
      buildSpec: codebuild.BuildSpec.fromSourceFilename("apps/infra/codebuild/buildspec-deploy-dev.yml"),
      environment: {
        buildImage: codebuild.LinuxBuildImage.STANDARD_7_0,
        // 4 vCPU / 7 GB: `next build` in the Vercel step does not fit small's 3 GB.
        computeType: codebuild.ComputeType.MEDIUM,
        // Docker daemon: CDK's Lambda bundling images and the apps/backend image asset.
        privileged: true,
      },
      // A deploy is 25-40 min (see the PR); CodeBuild's default cap is 60.
      timeout: cdk.Duration.minutes(90),
      logging: {
        cloudWatch: {
          logGroup: new logs.LogGroup(this, "Logs", {
            logGroupName: `/aws/codebuild/lightbulb-${envName}-deploy`,
            retention: logs.RetentionDays.TWO_WEEKS,
            removalPolicy: cdk.RemovalPolicy.DESTROY,
          }),
        },
      },
      // Deliberately no concurrentBuildLimit: CodeBuild does not queue past a
      // project limit ("new builds are throttled and are not run", CreateProject
      // API), so a burst of merges would drop the newest commit. deploy-dev.sh
      // serialises instead: older builds first, a newer build supersedes.
    });

    project.role?.attachInlinePolicy(
      new iam.Policy(this, "DeployPolicy", {
        policyName: `lightbulb-${envName}-deploy-runner`,
        document: iam.PolicyDocument.fromJson(
          JSON.parse(fs.readFileSync(path.join(codebuildDir, "role-policy.json"), "utf8")),
        ),
      }),
    );

    new cdk.CfnOutput(this, "StartBuild", {
      value: `aws codebuild start-build --project-name ${project.projectName} --source-version main`,
    });
  }
}
