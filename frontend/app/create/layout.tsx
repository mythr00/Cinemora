import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = { title: "Create a production", robots: { index: false, follow: true } };

export default function CreateLayout({ children }: { children: ReactNode }) {
  return children;
}
