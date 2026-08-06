import React from "react";
import type { Components } from "react-markdown";

/**
 * Shared markdown rendering components used by the chat ("Earl") and original
 * article pages so tables and other markup render identically everywhere.
 *
 * Extract the `markdownComponents` object that was previously defined inline in
 * the chat page so the article pages render with the exact same styled tables,
 * headings, lists, links, and code blocks.
 */
export const markdownComponents: Components = {
  table({ children }) {
    return (
      <div className="overflow-x-auto my-3">
        <table className="w-full text-xs border-collapse">{children}</table>
      </div>
    );
  },
  thead({ children }) {
    return <thead className="bg-white/10">{children}</thead>;
  },
  th({ children }) {
    return (
      <th className="px-3 py-2 text-left font-semibold text-earl-300 border-b border-white/10">
        {children}
      </th>
    );
  },
  td({ children }) {
    return <td className="px-3 py-1.5 border-b border-white/5">{children}</td>;
  },
  h1({ children }) {
    return <h1 className="text-base font-bold text-gray-100 mt-4 mb-1">{children}</h1>;
  },
  h2({ children }) {
    return <h2 className="text-sm font-bold text-gray-100 mt-4 mb-1">{children}</h2>;
  },
  h3({ children }) {
    return <h3 className="text-sm font-semibold text-gray-100 mt-3 mb-1">{children}</h3>;
  },
  hr() {
    return <hr className="border-white/10 my-4" />;
  },
  ul({ children }) {
    return <ul className="list-disc list-inside space-y-1 my-2">{children}</ul>;
  },
  ol({ children }) {
    return <ol className="list-decimal list-inside space-y-1 my-2">{children}</ol>;
  },
  p({ children }) {
    return <p className="text-gray-300 leading-relaxed mb-2 text-sm">{children}</p>;
  },
  a({ href, children }) {
    return (
      <a
        href={href}
        className="text-earl-400 hover:text-earl-300 underline"
        target="_blank"
        rel="noopener noreferrer"
      >
        {children}
      </a>
    );
  },
  strong({ children }) {
    return <strong className="font-bold text-gray-100">{children}</strong>;
  },
  code({ className, children, ...props }: React.ComponentPropsWithoutRef<"code">) {
    const isInline = !className;
    if (isInline) {
      return (
        <code className="bg-white/10 px-1 rounded text-xs" {...props}>
          {children}
        </code>
      );
    }
    return (
      <pre className="bg-black/40 rounded-lg p-3 my-3 overflow-x-auto text-xs">
        <code {...props}>{children}</code>
      </pre>
    );
  },
};
