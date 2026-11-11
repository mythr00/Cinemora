import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = { title: "Short-clip extraction", robots: { index: false, follow: true } };

export default function ViralClipsLayout({ children }: { children: ReactNode }) {
  return children;
}
