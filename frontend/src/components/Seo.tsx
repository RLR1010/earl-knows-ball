"use client";

import { useEffect } from "react";

// Meta-name attributes we manage. Any one we've already injected gets reused so
// we don't stack duplicate <meta> tags across client-side navigations.
const META_NAME_MGMT: Record<string, HTMLElement | null> = {
  description: null,
  keywords: null,
};

function ensureMeta(name: string, content: string | undefined) {
  let el = META_NAME_MGMT[name];
  if (!el) {
    el = document.head.querySelector<HTMLMetaElement>(`meta[name="${name}"]`);
    if (!el) {
      el = document.createElement("meta");
      el.setAttribute("name", name);
      document.head.appendChild(el);
    }
    META_NAME_MGMT[name] = el;
  }
  if (content) {
    el.setAttribute("content", content);
  } else {
    el.removeAttribute("content");
  }
}

export interface SeoInput {
  title?: string;
  description?: string;
  keywords?: string;
}

/**
 * Client-side SEO helper for Next.js pages that are React client components
 * (i.e. cannot use the `export const metadata` server API). Sets document.title
 * and the description/keywords meta tags after hydration.
 */
export function useSeo({ title, description, keywords }: SeoInput) {
  useEffect(() => {
    if (title) document.title = title;
    ensureMeta("description", description);
    ensureMeta("keywords", keywords);
  }, [title, description, keywords]);
}
