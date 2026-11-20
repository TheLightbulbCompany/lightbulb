import type { LiveEvent } from "../live/types";
import type { RuntimeOperation, RuntimeRpcOutcome } from "../runtime/types";

export interface RuntimeRpcSource {
  rpc<T = unknown>(method: string, params?: Record<string, unknown>): Promise<RuntimeRpcOutcome<T>>;
  getOperation(operationId: string): Promise<RuntimeOperation>;
  onEvent(handler: (event: LiveEvent) => void): () => void;
  connectionRevision: number;
}
