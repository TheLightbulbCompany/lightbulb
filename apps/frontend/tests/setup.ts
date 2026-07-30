import '@testing-library/jest-dom';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

// Mock ResizeObserver for jsdom (not available in jsdom)
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

// JSDOM polyfills for Radix UI primitives (Select, Dropdown, etc.).
// These methods aren't implemented in JSDOM but Radix calls them on pointer
// events, so user-event interaction with a Radix Select trigger throws
// "target.hasPointerCapture is not a function" without these stubs.
if (typeof Element !== 'undefined') {
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = vi.fn(() => false) as unknown as typeof Element.prototype.hasPointerCapture;
  }
  if (!Element.prototype.releasePointerCapture) {
    Element.prototype.releasePointerCapture = vi.fn() as unknown as typeof Element.prototype.releasePointerCapture;
  }
  if (!Element.prototype.setPointerCapture) {
    Element.prototype.setPointerCapture = vi.fn() as unknown as typeof Element.prototype.setPointerCapture;
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = vi.fn() as unknown as typeof Element.prototype.scrollIntoView;
  }
}

vi.mock('next/navigation', () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    prefetch: vi.fn(),
    back: vi.fn(),
  }),
  usePathname: () => '/',
  useSearchParams: () => new URLSearchParams(),
}));

const { mockGetToken } = vi.hoisted(() => ({
  mockGetToken: vi.fn(() => Promise.resolve('mock-jwt-token')),
}));

vi.mock('@clerk/nextjs', () => ({
  useAuth: () => ({
    isSignedIn: true,
    isLoaded: true,
    userId: 'user_test_123',
    getToken: mockGetToken,
  }),
  useUser: () => ({
    isSignedIn: true,
    isLoaded: true,
    user: {
      id: 'user_test_123',
      primaryEmailAddress: { emailAddress: 'test@example.com' },
      firstName: 'Test',
      lastName: 'User',
    },
  }),
  useOrganization: () => ({
    organization: null,
    isLoaded: true,
  }),
  useOrganizationList: () => ({
    setActive: vi.fn(),
    userMemberships: { data: [], isLoading: false },
    isLoaded: true,
  }),
  OrganizationSwitcher: () => null,
  UserButton: () => null,
  SignedIn: ({ children }: { children: React.ReactNode }) => children,
  SignedOut: () => null,
  ClerkProvider: ({ children }: { children: React.ReactNode }) => children,
}));

// useSupport() throws outside a <SupportProvider>. ConnectButton (and other
// surfaces) call it unconditionally, so — like the Clerk mock above — stub it
// globally rather than making every unrelated test wrap its tree in a real
// provider. A test asserting on openSupport's call args (e.g. the "Request
// access" flow) overrides this with its own vi.mock of the same module.
vi.mock('@/components/support/SupportProvider', () => ({
  useSupport: () => ({ openSupport: vi.fn(), closeSupport: vi.fn() }),
}));
