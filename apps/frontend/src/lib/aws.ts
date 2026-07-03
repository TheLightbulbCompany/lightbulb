// Single source for the AWS region used in console-URL construction.
// Override per-env with NEXT_PUBLIC_AWS_REGION; defaults to us-east-1.
export const AWS_REGION: string = process.env.NEXT_PUBLIC_AWS_REGION ?? "us-east-1";

export interface EcsTaskRef {
  task_arn?: string;
  service_name?: string;
  cluster_name?: string;
  cluster_arn?: string;
}

/**
 * Build an ECS console deep-link for a user's container task.
 *
 * The cluster name component of the URL is best-effort: backend rows store
 * just `cluster_arn` in some cases. We extract the trailing segment when an
 * ARN is present and fall back to a generic console link otherwise so the
 * operator can still pivot.
 */
export function buildEcsConsoleUrl(c: EcsTaskRef): string | null {
  if (!c.task_arn) return null;
  const region = AWS_REGION;
  const cluster =
    c.cluster_name ??
    (c.cluster_arn ? c.cluster_arn.split("/").pop() : undefined);
  if (cluster && c.service_name) {
    return `https://${region}.console.aws.amazon.com/ecs/v2/clusters/${encodeURIComponent(cluster)}/services/${encodeURIComponent(c.service_name)}/tasks?region=${region}`;
  }
  return `https://${region}.console.aws.amazon.com/ecs/v2/clusters?region=${region}`;
}
