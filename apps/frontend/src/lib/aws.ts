// Single source for the AWS region used in console-URL construction.
// Override per-env with NEXT_PUBLIC_AWS_REGION; defaults to us-east-1.
export const AWS_REGION: string = process.env.NEXT_PUBLIC_AWS_REGION ?? "us-east-1";
