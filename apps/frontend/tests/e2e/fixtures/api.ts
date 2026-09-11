import type { Page } from '@playwright/test';

/**
 * Typed error thrown by AuthedFetch on non-2xx responses. Exposes the HTTP
 * status as a structured field so callers can branch on the actual response
 * code rather than scanning the message text (Codex P2 on PR #309 — a 500
 * whose body happens to mention "404" must NOT be treated as idempotent).
 */
export class AuthedFetchError extends Error {
  readonly status: number;
  readonly path: string;
  readonly method: string;
  readonly body: string;
  constructor(method: string, path: string, status: number, body: string) {
    super(`${method} ${path} ${status}: ${body}`);
    this.name = 'AuthedFetchError';
    this.method = method;
    this.path = path;
    this.status = status;
    this.body = body;
  }
}

/**
 * Page-aware authenticated fetch. Reads a fresh Clerk JWT from the page on
 * every call (Clerk tokens expire after 60s). Adds X-E2E-Run-Id for backend
 * log correlation.
 */
export class AuthedFetch {
  constructor(
    private page: Page,
    private apiUrl: string,
    private runId: string,
  ) {}

  private async token(): Promise<string> {
    // Throw eagerly if Clerk isn't loaded on the current page. Falling back
    // to '' would send `Authorization: Bearer ` and the backend would 401 —
    // catastrophic during cleanup if a spec failed while the tab was on
    // Stripe Checkout (or any non-app page) because the 401 would abort
    // teardown before Clerk org/user delete ran (Codex P1 on PR #309).
    // Callers that hit this should navigate the page back to BASE_URL first.
    const token = await this.page.evaluate(async () => {
      const w = window as Window & {
        Clerk?: { session?: { getToken: () => Promise<string> } };
      };
      return (await w.Clerk?.session?.getToken()) ?? null;
    });
    if (!token) {
      throw new Error(
        `AuthedFetch.token: no Clerk session on current page (url=${this.page.url()}). ` +
          `Navigate back to a Clerk-enabled URL before issuing authenticated requests.`,
      );
    }
    return token;
  }

  private async send(method: string, path: string, body?: unknown): Promise<Response> {
    // Retry on transient 502/503/504 — the gateway proxies RPC to the
    // user's OpenClaw container over WebSocket, and during an upgrade
    // (free→Workspace) the container is reconfigured (model swap) and the
    // gateway briefly returns "Gateway RPC call failed" (502). Up to 5
    // attempts with 2s backoff covers typical reconfig windows. Non-5xx
    // errors return immediately.
    for (let attempt = 0; ; attempt++) {
      const token = await this.token();
      const res = await fetch(`${this.apiUrl}${path}`, {
        method,
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/json',
          'X-E2E-Run-Id': this.runId,
        },
        body: body ? JSON.stringify(body) : undefined,
      });
      const transient = res.status === 502 || res.status === 503 || res.status === 504;
      if (!transient || attempt >= 4) return res;
      await new Promise((r) => setTimeout(r, 2_000));
    }
  }

  private async unwrap<T>(method: string, path: string, res: Response): Promise<T> {
    if (!res.ok) {
      throw new AuthedFetchError(method, path, res.status, await res.text());
    }
    return res.json() as Promise<T>;
  }

  async get<T = unknown>(path: string): Promise<T> {
    return this.unwrap<T>('GET', path, await this.send('GET', path));
  }

  async post<T = unknown>(path: string, body?: unknown): Promise<T> {
    return this.unwrap<T>('POST', path, await this.send('POST', path, body));
  }

  async delete<T = unknown>(path: string): Promise<T> {
    return this.unwrap<T>('DELETE', path, await this.send('DELETE', path));
  }
}
