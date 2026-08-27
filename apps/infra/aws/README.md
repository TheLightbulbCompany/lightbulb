# Lightbulb AI AWS connector

`aws-connector.yaml` is the CloudFormation template an owner launches
in **their own** AWS account to create a scoped role that Lightbulb AI's backend can
assume on their behalf.

- It is launched via the CloudFormation console **quick-create link** that the
  `connections/{provider}/aws-setup` endpoint builds (see
  `apps/backend/core/services/credentials/aws_cfn.py`). The link prefills the
  `ExternalId`, `TrustedPrincipalArn`, and `ManagedPolicyArn` parameters.
- `ManagedPolicyArn` is constrained via `AllowedValues` to exactly the three
  offered scope tiers — `job-function/ViewOnlyAccess`, `ReadOnlyAccess`, and
  `PowerUserAccess` — so an owner can't fat-finger a broader policy
  (e.g. `AdministratorAccess`) onto the role.
- The template is hosted at the public URL configured via `CFN_TEMPLATE_URL`
  (falling back to `aws_sts.cfn_template_url` in
  `apps/backend/core/services/credentials/providers.yaml`).
- **Trust:** the role trusts Lightbulb AI's platform principal — our account root
  (`arn:aws:iam::877352799272:root`) — **and** requires the per-connection
  `sts:ExternalId` on every AssumeRole call. Lightbulb AI's backend performs the
  assume and vends the short-lived credentials to the owner's container via
  its `credential_process` endpoint; the container itself holds no AWS
  identity.
- Because the role uses a fixed name (`isol8-connector`), launching the stack
  requires acknowledging **CAPABILITY_NAMED_IAM**.

After the stack is created, the owner copies the `RoleArn` stack output back
into Lightbulb AI (`aws-complete`) to finish the connection.
