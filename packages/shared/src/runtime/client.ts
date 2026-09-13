import type { ApiMethods } from "../api/client";
import { ApiError } from "../api/error";
import type { RuntimeOperation, RuntimeRpcOutcome } from "./types";

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

export class RuntimeRpcError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly retryable: boolean;
  readonly retryAfter?: number;
  readonly details?: Record<string, unknown>;

  constructor(error: {
    status: number;
    code?: string;
    message: string;
    retryable?: boolean;
    retryAfter?: number;
    details?: Record<string, unknown>;
  }) {
    super(error.message);
    this.name = "RuntimeRpcError";
    this.status = error.status;
    this.code = error.code;
    this.retryable = error.retryable === true;
    this.retryAfter = error.retryAfter;
    this.details = error.details;
  }

  static from(error: ApiError): RuntimeRpcError {
    const body = record(error.body);
    const detail = record(body?.detail);
    const value = detail ?? body;
    return new RuntimeRpcError({
      status: error.status,
      code: typeof value?.code === "string" ? value.code : undefined,
      message: typeof value?.message === "string" && value.message ? value.message : error.message,
      retryable: value?.retryable === true,
      retryAfter: typeof value?.retryAfter === "number" && value.retryAfter > 0 ? value.retryAfter : undefined,
      details: record(value?.details) ?? undefined,
    });
  }
}

function invalidResponse(): RuntimeRpcError {
  return new RuntimeRpcError({
    status: 500,
    code: "INTERNAL_ERROR",
    message: "Runtime returned an invalid response.",
    retryable: false,
  });
}

async function translate<T>(request: Promise<unknown>): Promise<T> {
  try {
    return await request as T;
  } catch (error) {
    if (error instanceof ApiError) throw RuntimeRpcError.from(error);
    throw error;
  }
}

export interface RuntimeClient {
  rpc<T = unknown>(method: string, params?: Record<string, unknown>): Promise<RuntimeRpcOutcome<T>>;
  getOperation(operationId: string): Promise<RuntimeOperation>;
}

export function createRuntimeClient(api: ApiMethods, uuid: () => string): RuntimeClient {
  return {
    async rpc<T>(method: string, params = {}): Promise<RuntimeRpcOutcome<T>> {
      const response = record(await translate<unknown>(api.post("/runtime/rpc", {
        requestId: uuid(),
        method,
        params,
      })));
      if (typeof response?.operationId === "string" && response.operationId) {
        return { kind: "operation", operationId: response.operationId };
      }
      if (response?.ok === true && Object.hasOwn(response, "payload")) {
        return { kind: "result", value: response.payload as T };
      }
      throw invalidResponse();
    },
    getOperation: (operationId) => translate<RuntimeOperation>(
      api.get(`/runtime/operations/${encodeURIComponent(operationId)}`),
    ),
  };
}
