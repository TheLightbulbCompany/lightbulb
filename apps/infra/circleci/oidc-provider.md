# CircleCI OIDC for the dev deploy

`.circleci/deploy-dev.yml` authenticates to AWS with CircleCI's OIDC token and
assumes `arn:aws:iam::877352799272:role/lightbulb-circleci-deploy`, which may
only assume the CDK bootstrap roles (`cdk-hnb659fds-*-877352799272-us-east-1`).
Nothing is long-lived: no access keys anywhere, and the GitHub Actions role
(`lightbulb-github-actions`) is untouched for prod promotion.

Identifiers (CircleCI, GitHub App integration):

| | |
|---|---|
| org id | `Jt5rbVSvEdDxeXLX3bvu6H` |
| project id | `b6acec1b-6ba0-4b3c-8d65-0ac8d5178503` (project `lightbulb`) |
| issuer | `https://oidc.circleci.com/org/Jt5rbVSvEdDxeXLX3bvu6H` |
| token audience | the org id |
| token subject (V2) | `org/<org>/project/<project>/user/<user>/vcs-origin/github.com/TheLightbulbCompany/lightbulb/vcs-ref/refs/heads/main` |

## 1. Identity provider (once per account)

```sh
AWS_PROFILE=isol8-admin aws iam create-open-id-connect-provider \
  --url https://oidc.circleci.com/org/Jt5rbVSvEdDxeXLX3bvu6H \
  --client-id-list Jt5rbVSvEdDxeXLX3bvu6H
```

The provider ARN is `arn:aws:iam::877352799272:oidc-provider/oidc.circleci.com/org/Jt5rbVSvEdDxeXLX3bvu6H`
(the URL minus `https://`), which `trust-policy.json` names as the federated
principal. `--thumbprint-list` is optional: IAM retrieves the CA thumbprint
itself (CLI help text). Check with
`aws iam list-open-id-connect-providers`.

## 2. Role

`apply.sh` runs the command above and then:

- `aws iam create-role lightbulb-circleci-deploy` with `trust-policy.json`
  and `--max-session-duration 7200`;
- `aws iam put-role-policy` with `permissions-policy.json` (inline, same name).

### trust-policy.json

- `aud` must equal the org id (CircleCI's fixed default audience).
- `sub` is pinned to this project and to jobs running `refs/heads/main` through
  the repo's GitHub origin -- a PR branch pipeline cannot assume the deploy
  role even though the same project runs it. If the first deploy fails at
  "Assume Role with Web Identity", decode `CIRCLE_OIDC_TOKEN_V2`'s `sub` from
  the job (`echo "$CIRCLE_OIDC_TOKEN_V2" | cut -d. -f2 | base64 -d`) and fix
  the suffix; the loosest acceptable value is
  `org/Jt5rbVSvEdDxeXLX3bvu6H/project/b6acec1b-6ba0-4b3c-8d65-0ac8d5178503/user/*`.

### permissions-policy.json

What `lightbulb-github-actions` has, minus what the dev deploy does not use:

| GitHub role grant | here | why |
|---|---|---|
| `sts:AssumeRole` on `role/cdk-*` | yes, as `role/cdk-hnb659fds-*-877352799272-us-east-1` | `cdk deploy` assumes the file/image-publishing roles to publish assets and the deploy role to run CloudFormation (which executes as the cfn-exec role). The qualifier and region pin it to this bootstrap. |
| `sts:TagSession` on `role/cdk-*` | no | `aws-actions/configure-aws-credentials` tags sessions; the CDK CLI does not. |
| `CdkOutTransport` (S3 `gha-cdk-out/*` on the assets bucket) | no | moved `cdk.out` between GitHub jobs; the CircleCI synth and deploy are one job. |

The bootstrap roles are shared by dev and prod, so IAM cannot scope this
role to dev; what keeps it dev-only is the trust policy (main-branch jobs
only) and the one `cdk deploy ... 'dev/*'` the config runs.
