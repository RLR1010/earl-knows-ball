"use client";

import Image from "next/image";
import { getTeamLogoUrl } from "@/lib/team_logos";

/**
 * Shared team logo renderer.
 *
 * Renders the primary team logo (MLB/NBA SVG or NFL local PNG) on its own,
 * unframed, with a soft white glow filter so light/white elements stay
 * readable on the dark site background.
 */
export default function TeamLogo({
  abbr,
  sport,
  name,
  chip = false,
  size = 32,
  className = "",
  style,
}: {
  abbr: string;
  sport: string;
  name?: string;
  /** @deprecated chips are no longer used; kept for API compatibility. */
  chip?: boolean;
  size?: number;
  className?: string;
  style?: React.CSSProperties;
}) {
  const alt = name ?? abbr;
  const logoUrl = getTeamLogoUrl(abbr, sport);

  if (!logoUrl) {
    return (
      <span
        className={`flex items-center justify-center shrink-0 ${className}`}
        style={{ width: size, height: size, ...style }}
      >
        <span className="text-[10px] font-semibold text-gray-400">{abbr}</span>
      </span>
    );
  }

  const isNfl = sport === "nfl";
  // Pin the logo to the box with object-contain so non-square SVG viewBoxes
  // can't inflate height beyond `size`.
  const rendered = isNfl ? (
    <Image
      src={logoUrl}
      alt={alt}
      width={size}
      height={size}
      className="object-contain"
      style={{
        width: size,
        height: size,
        filter:
          "brightness(1.1) drop-shadow(0 0 1px #fff) drop-shadow(0 0 2px #ffffffaa) drop-shadow(0 0 3px #000)",
      }}
    />
  ) : (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={logoUrl}
      alt={alt}
      width={size}
      height={size}
      className="object-contain"
      style={{
        width: size,
        height: size,
        filter:
          "brightness(1.1) drop-shadow(0 0 1px #fff) drop-shadow(0 0 2px #ffffffaa) drop-shadow(0 0 3px #000)",
      }}
    />
  );

  return (
    <span
      className={`flex items-center justify-center shrink-0 ${className}`}
      style={{ width: size, height: size, ...style }}
      title={alt}
    >
      {rendered}
    </span>
  );
}
