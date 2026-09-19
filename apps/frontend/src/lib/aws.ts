// Single source for the AWS region used in console-URL construction.
// Override per-env with NEXT_PUBLIC_AWS_REGION; defaults to us-east-1.
//
// `buildEcsConsoleUrl` lived here and is deleted with the ECS runtime: a workspace is an
// AgentCore runtime now, which has no per-workspace console page to deep-link. The admin
// container card renders the runtime ARN instead — what an operator pastes into
// `aws bedrock-agentcore get-agent-runtime`.
export const AWS_REGION: string = process.env.NEXT_PUBLIC_AWS_REGION ?? "us-east-1";
