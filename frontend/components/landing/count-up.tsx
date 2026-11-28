"use client";

import { useEffect, useRef, useState } from "react";

/**
 * CountUp (design philosophy transformation, 2026-11-28).
 * A number that counts when it enters the viewport — used
 * where a figure is itself the demonstration (the narration
 * panel's 54 voices / 8 languages). rAF ease-out, tabular
 * figures so nothing jitters, one-shot. Under
 * prefers-reduced-motion — or without IntersectionObserver —
 * the final value is simply shown.
 */
export function CountUp({
  value,
  duration = 1200,
  className,
}: {
  value: number;
  duration?: number;
  className?: string;
}) {
  const ref = useRef<HTMLSpanElement | null>(null);
  const [shown, setShown] = useState(0);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (
      window.matchMedia("(prefers-reduced-motion: reduce)").matches ||
      !("IntersectionObserver" in window)
    ) {
      const id = requestAnimationFrame(() => setShown(value));
      return () => cancelAnimationFrame(id);
    }
    let raf = 0;
    let started = false;
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting || started) continue;
          started = true;
          const t0 = performance.now();
          const step = (now: number) => {
            const t = Math.min(1, (now - t0) / duration);
            const eased = 1 - Math.pow(1 - t, 3);
            setShown(Math.round(eased * value));
            if (t < 1) raf = requestAnimationFrame(step);
          };
          raf = requestAnimationFrame(step);
          io.disconnect();
        }
      },
      { threshold: 0.4 },
    );
    io.observe(el);
    return () => {
      io.disconnect();
      cancelAnimationFrame(raf);
    };
  }, [value, duration]);

  return (
    <span ref={ref} className={className} style={{ fontVariantNumeric: "tabular-nums" }}>
      {shown}
    </span>
  );
}
