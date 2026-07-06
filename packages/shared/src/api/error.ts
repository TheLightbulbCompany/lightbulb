/**
 * Thrown by the API client helpers when the backend returns a non-2xx
 * response.
 *
 * `body` is the parsed JSON response body when Content-Type is JSON, else
 * null. Callers can switch on `status` and `body` to handle structured
 * error payloads (e.g. the `blocked` field from /container/* endpoints).
 *
 * `requestId` is the backend's `X-Request-ID` for the failing response (null
 * if the header was absent — e.g. cross-origin before CORS exposed it, or a
 * network failure with no response). The Report button on an error toast
 * forwards it so support tickets pin the exact failing request.
 */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly body: unknown,
    message?: string,
    public readonly requestId?: string | null,
  ) {
    super(message ?? `API ${status}`);
    this.name = "ApiError";
  }
}
