import type { AuthedFetch } from '../fixtures/api';

type StatusResponse = { container_status?: string; container_substatus?: string | null };

export async function containerHealthy(
  api: AuthedFetch,
  opts: { timeoutMs?: number } = {},
): Promise<void> {
  const deadline = Date.now() + (opts.timeoutMs ?? 10 * 60_000);
  let last: string | undefined;
  while (Date.now() < deadline) {
    let data: StatusResponse | null = null;
    try {
      data = await api.get<StatusResponse>('/container/status');
    } catch (err) {
      // 404 = no container yet (ProvisioningStepper hasn't fired POST
      // /container/provision); 503 = ECS rolling update mid-flight. Both
      // are normal during cold-start — silently retry. Anything else gets
      // logged but still retried (network blips shouldn't fail the gate).
      const msg = String(err);
      if (!msg.includes('503') && !msg.includes('404')) {
        console.warn('[e2e] container status err:', err);
      }
      await new Promise((r) => setTimeout(r, 3000));
      continue;
    }

    if (data.container_substatus === 'gateway_healthy') return;
    last = `${data.container_status}/${data.container_substatus}`;
    // Terminal error state — fail fast instead of polling for the full 10
    // minutes (Codex P2 on PR #309). The catch block above can't see this
    // throw because it's outside the try.
    if (data.container_status === 'error') {
      throw new Error(`Container in error state: ${last}`);
    }

    await new Promise((r) => setTimeout(r, 3000));
  }
  throw new Error(`containerHealthy: timed out, last status=${last}`);
}
