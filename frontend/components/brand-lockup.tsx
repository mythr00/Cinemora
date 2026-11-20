import Image from "next/image";

/**
 * The ONE brand rendering in the product (redesign plan §3.14).
 *
 * Canonical mark: `public/cinemora-mark.svg` beside the word
 * "Cinemora" set in Inter. Sizing is a component API, never
 * contextual CSS — no page, layout or component may render its
 * own brand, wordmark image or text-only brand.
 *
 * - `app`       — the studio shell (mark 32px, word 19px)
 * - `marketing` — the landing header/footer (mark 38px, word 21px)
 * - `compact`   — mark only, 28px (collapsed rail / phone)
 */
const SIZES = {
  app: { mark: 32, word: true },
  marketing: { mark: 38, word: true },
  compact: { mark: 28, word: false },
} as const;

export type BrandLockupSize = keyof typeof SIZES;

export function BrandLockup({
  size = "app",
  priority = false,
}: {
  size?: BrandLockupSize;
  priority?: boolean;
}) {
  const spec = SIZES[size];
  return (
    <span className={`brand-lockup brand-lockup--${size}`}>
      <Image
        src="/cinemora-mark.svg"
        alt=""
        width={spec.mark}
        height={spec.mark}
        priority={priority}
      />
      {spec.word && <span className="brand-word">Cinemora</span>}
    </span>
  );
}
