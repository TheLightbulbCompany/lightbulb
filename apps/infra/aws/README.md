# Isol8 AWS read-only connector

`aws-readonly-connector.yaml` is the CloudFormation template an owner launches in
**their own** AWS account to create a scoped, read-only role that Isol8's
per-owner agent container can assume.

- It is launched via the CloudFormation console **quick-create link** that the
  `connections/{provider}/aws-setup` endpoint builds (see
  `apps/backend/core/services/credentials/aws_cfn.py`). The link prefills the
  `ExternalId`, `TrustedPrincipalArn`, and `ManagedPolicyArn` parameters.
- `ManagedPolicyArn` is constrained via `AllowedValues` to exactly the two
  intended AWS managed read-only policies —
  `arn:aws:iam::aws:policy/job-function/ViewOnlyAccess` and
  `arn:aws:iam::aws:policy/ReadOnlyAccess` — so an owner can't fat-finger a
  broader policy (e.g. `AdministratorAccess`) onto the role.
- The template is hosted at the public URL configured in
  `apps/backend/core/services/credentials/providers.yaml` under
  `aws_sts.cfn_template_url` (wired in a separate deploy step — not here).
- The role is **trust-pinned**: it can be assumed only by the owner's Isol8
  container task role (`TrustedPrincipalArn`) **and** only when the call carries
  the matching `sts:ExternalId`.
- Because the role uses a fixed name (`isol8-connector`), launching the
  stack requires acknowledging **CAPABILITY_NAMED_IAM**.

After the stack is created, the owner copies the `RoleArn` stack output back into
Isol8 (`aws-complete`) to finish the connection.
