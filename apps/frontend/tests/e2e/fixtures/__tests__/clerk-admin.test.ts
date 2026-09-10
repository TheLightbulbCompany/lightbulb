import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createUser, deleteUser, findUserByEmail, deleteOrg } from '../clerk-admin';

const FAKE_KEY = 'sk_test_fake';

describe('clerk-admin', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    global.fetch = fetchMock as unknown as typeof fetch;
  });

  describe('createUser', () => {
    it('POSTs the right payload and returns the user ID', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: 'user_abc123' }),
      });

      const id = await createUser({
        secretKey: FAKE_KEY,
        email: 'isol8-e2e-aaaaaa@mailsac.com',
        password: 'pw',
        runId: '1776572400000-aaaaaa',
      });

      expect(id).toBe('user_abc123');
      expect(fetchMock).toHaveBeenCalledWith(
        'https://api.clerk.com/v1/users',
        expect.objectContaining({
          method: 'POST',
          headers: expect.objectContaining({
            Authorization: 'Bearer sk_test_fake',
          }),
        }),
      );
      const body = JSON.parse(fetchMock.mock.calls[0][1].body);
      expect(body.email_address).toEqual(['isol8-e2e-aaaaaa@mailsac.com']);
      expect(body.password).toBe('pw');
      expect(body.unsafe_metadata.e2e_run_id).toBe('1776572400000-aaaaaa');
    });

    it('throws on non-2xx with the API body in the message', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: false,
        status: 422,
        text: async () => '{"errors":[{"code":"form_password_pwned"}]}',
      });
      await expect(
        createUser({ secretKey: FAKE_KEY, email: 'x@y.com', password: 'pw', runId: 'r' }),
      ).rejects.toThrow(/422.*form_password_pwned/);
    });
  });

  describe('findUserByEmail', () => {
    it('filters in JS, never trusts the email_address[] query', async () => {
      // Reproduce the bug from PR #300: Clerk returns ALL users.
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: async () => [
          {
            id: 'user_other',
            email_addresses: [{ email_address: 'someone-else@example.com' }],
          },
          {
            id: 'user_target',
            email_addresses: [{ email_address: 'isol8-e2e-target@mailsac.com' }],
          },
        ],
      });

      const result = await findUserByEmail({
        secretKey: FAKE_KEY,
        email: 'isol8-e2e-target@mailsac.com',
      });
      expect(result?.id).toBe('user_target');
    });

    it('returns null when no user matches', async () => {
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: async () => [
          { id: 'user_other', email_addresses: [{ email_address: 'else@x.com' }] },
        ],
      });
      const result = await findUserByEmail({
        secretKey: FAKE_KEY,
        email: 'missing@x.com',
      });
      expect(result).toBeNull();
    });

    it('paginates and finds a match on a later page', async () => {
      // Codex P2 on PR #309: previously fetched only the first page, so a
      // leaked user beyond the first 10/500 would be missed. Simulate a full
      // first page (limit=500) followed by a second page that contains the
      // target.
      const fullFirstPage = Array.from({ length: 500 }, (_, i) => ({
        id: `user_other_${i}`,
        email_addresses: [{ email_address: `noise${i}@example.com` }],
      }));
      fetchMock
        .mockResolvedValueOnce({ ok: true, json: async () => fullFirstPage })
        .mockResolvedValueOnce({
          ok: true,
          json: async () => [
            {
              id: 'user_target',
              email_addresses: [{ email_address: 'isol8-e2e-target@mailsac.com' }],
            },
          ],
        });

      const result = await findUserByEmail({
        secretKey: FAKE_KEY,
        email: 'isol8-e2e-target@mailsac.com',
      });
      expect(result?.id).toBe('user_target');
      expect(fetchMock).toHaveBeenCalledTimes(2);
      // Page-2 request used offset=500
      expect(fetchMock.mock.calls[1][0]).toContain('offset=500');
    });

    it('returns null after exhausting all pages with no match', async () => {
      // Page returns < limit rows → last page → return null.
      fetchMock.mockResolvedValueOnce({
        ok: true,
        json: async () => [
          { id: 'user_a', email_addresses: [{ email_address: 'a@x.com' }] },
        ],
      });
      const result = await findUserByEmail({
        secretKey: FAKE_KEY,
        email: 'missing@x.com',
      });
      expect(result).toBeNull();
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
  });

  describe('deleteUser', () => {
    it('DELETEs the right URL', async () => {
      fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({}) });
      await deleteUser({ secretKey: FAKE_KEY, userId: 'user_abc' });
      expect(fetchMock).toHaveBeenCalledWith(
        'https://api.clerk.com/v1/users/user_abc',
        expect.objectContaining({ method: 'DELETE' }),
      );
    });

    it('treats 404 as success (idempotent)', async () => {
      fetchMock.mockResolvedValueOnce({ ok: false, status: 404, text: async () => '' });
      await expect(
        deleteUser({ secretKey: FAKE_KEY, userId: 'user_gone' }),
      ).resolves.not.toThrow();
    });
  });

  describe('deleteOrg', () => {
    it('DELETEs the right URL', async () => {
      fetchMock.mockResolvedValueOnce({ ok: true, json: async () => ({}) });
      await deleteOrg({ secretKey: FAKE_KEY, orgId: 'org_abc' });
      expect(fetchMock).toHaveBeenCalledWith(
        'https://api.clerk.com/v1/organizations/org_abc',
        expect.objectContaining({ method: 'DELETE' }),
      );
    });
  });
});
