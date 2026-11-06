import '@testing-library/jest-dom';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';
import { installStoragePolyfill } from '../src/test-utils/storagePolyfill';

// Web storage, once, for every test file.
//
// Two things were wrong before. Whether `localStorage` works at all depended on
// the Node version — jsdom's Storage is live on Node 20 (what CI pins) but
// shadowed/absent on Node 22+ (what a dev machine may run), so storage-backed
// code paths silently no-op locally and run for real in CI. And nothing reset
// it between tests, so anything a test persisted leaked into the next one in
// the same file: the inbox tab, the active chat session. That is invisible
// while storage is a no-op and it is 13 failures when it isn't.
//
// Installing the Map-backed polyfill makes the behaviour identical everywhere;
// clearing it in afterEach makes each test start from an empty store. A file
// that installs its own polyfill in `beforeAll` still overrides this one.
installStoragePolyfill();

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
  localStorage.clear();
  sessionStorage.clear();
});

// Mock ResizeObserver for jsdom (not available in jsdom)
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

// matchMedia for jsdom (not implemented there either). Components read
// prefers-reduced-motion on mount, so without this any test rendering one
// throws "window.matchMedia is not a function". Default is "nothing matches";
// a test that needs a query to match assigns its own implementation.
if (typeof window !== 'undefined' && !window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

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
// Lexical (the room composer) measures the selection to keep the caret in view; jsdom's Range has no layout.
if (typeof Range !== "undefined" && !Range.prototype.getBoundingClientRect) {
  Range.prototype.getBoundingClientRect = () => new DOMRect();
  Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
}

// JSDOM has no PointerEvent constructor at all (unlike the capture methods
// above, which exist but throw), so `fireEvent.pointerDown/Move/Up` fall back
// to a plain `Event` that silently drops `clientX`/`buttons`/`pointerId` --
// any onPointer* handler reading those sees `undefined`. A MouseEvent subclass
// covers the fields ColumnGrip-style drag handling needs.
if (typeof window !== 'undefined' && !window.PointerEvent) {
  class PointerEventPolyfill extends MouseEvent {
    pointerId: number;
    pointerType: string;
    constructor(type: string, params: PointerEventInit = {}) {
      super(type, params);
      this.pointerId = params.pointerId ?? 0;
      this.pointerType = params.pointerType ?? 'mouse';
    }
  }
  window.PointerEvent = PointerEventPolyfill as unknown as typeof PointerEvent;
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
//
// Spread the real module first: stubbing ONLY useSupport would drop the
// <SupportProvider> component export, so any test that renders a tree
// containing it would get `undefined is not a component` with no clue why.
vi.mock('@/components/support/SupportProvider', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useSupport: () => ({ openSupport: vi.fn(), closeSupport: vi.fn() }),
}));
