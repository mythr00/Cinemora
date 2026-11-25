/**
 * Local draft persistence for creation forms (Phase 6, A-05).
 *
 * Creation forms hold scripts of up to 60,000 characters; a
 * refresh used to destroy all of it. Drafts now persist to
 * localStorage as the user types (debounced by the callers) and
 * restore on load. Every helper is fail-soft: storage can throw
 * (private mode, quota, disabled cookies/storage) and that must
 * NEVER block typing or submitting — a failed save simply means
 * no draft, exactly the old behaviour.
 */

export function loadDraft<T>(key: string): T | null {
  try {
    if (typeof window === "undefined") return null;
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

export function saveDraft(key: string, value: unknown): void {
  try {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Private mode / quota: the form keeps working without a draft.
  }
}

export function clearDraft(key: string): void {
  try {
    if (typeof window === "undefined") return;
    window.localStorage.removeItem(key);
  } catch {
    // Nothing to clear if storage is unavailable.
  }
}
