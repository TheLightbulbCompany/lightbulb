export interface RuntimeError {
  code: string;
  message: string;
  retryable: boolean;
}

export interface RuntimeOperation {
  operationId: string;
  state: "queued" | "running" | "succeeded" | "failed";
  createdAt: string;
  updatedAt: string;
  result: unknown | null;
  error: RuntimeError | null;
}

export interface RuntimeRpcRequest {
  requestId: string;
  method: string;
  params: Record<string, unknown>;
}

export type RuntimeRpcOutcome<T> =
  | { kind: "result"; value: T }
  | { kind: "operation"; operationId: string };
