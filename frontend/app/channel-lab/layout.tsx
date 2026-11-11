import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = { title: "Script", robots: { index: false, follow: true } };

export default function ChannelLabLayout({ children }: { children: ReactNode }) {
  return children;
}
