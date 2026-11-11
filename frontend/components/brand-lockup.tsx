import Image from "next/image";

export function BrandLockup({ priority = false }: { priority?: boolean }) {
  return <span className="brand-lockup"><Image src="/cinemora-mark.svg" alt="" width={38} height={38} priority={priority} /><span className="brand-word">Cinemora</span></span>;
}
