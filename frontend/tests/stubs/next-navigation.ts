/**
 * Test stub for next/navigation (aliased in vitest.config.ts).
 * Components under test use the pathname/router/params hooks for
 * chrome only; route state is not part of the behaviour asserted.
 */
export function usePathname(): string {
  return "/";
}

export function useRouter() {
  return {
    push: () => undefined,
    replace: () => undefined,
    back: () => undefined,
    forward: () => undefined,
    refresh: () => undefined,
    prefetch: () => undefined,
  };
}

export function useParams(): Record<string, string> {
  return {};
}

export function useSearchParams(): URLSearchParams {
  return new URLSearchParams();
}

export function useSelectedLayoutSegment(): string | null {
  return null;
}

export function redirect(): never {
  throw new Error("redirect() is not available in tests");
}

export function notFound(): never {
  throw new Error("notFound() is not available in tests");
}
