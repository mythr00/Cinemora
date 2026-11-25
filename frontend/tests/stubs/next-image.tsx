/**
 * Test stub for next/image (aliased in vitest.config.ts): renders
 * a plain <img> with the same src/alt, dropping Next-only props
 * (priority, fill, loader…) that jsdom cannot honour anyway.
 */
import type { ImgHTMLAttributes } from "react";

type NextImageProps = ImgHTMLAttributes<HTMLImageElement> & {
  src: string;
  priority?: boolean;
  fill?: boolean;
  loader?: unknown;
  quality?: number | string;
  placeholder?: string;
  blurDataURL?: string;
  unoptimized?: boolean;
};

export default function Image({
  src,
  alt,
  priority: _priority,
  fill: _fill,
  loader: _loader,
  quality: _quality,
  placeholder: _placeholder,
  blurDataURL: _blurDataURL,
  unoptimized: _unoptimized,
  ...rest
}: NextImageProps) {
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={src} alt={alt ?? ""} {...rest} />;
}
