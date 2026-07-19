"use client";

import Link from "next/link";

export default function Footer() {
  return (
    <footer className="border-t border-white/10 bg-gray-950 mt-auto">
      <div className="max-w-7xl mx-auto px-4 py-8">
        {/* Links */}
        <div className="flex items-center justify-center gap-6 mb-6 text-sm">
          <Link href="/faq" className="text-gray-400 hover:text-white transition">
            FAQ
          </Link>
          <Link href="/terms" className="text-gray-400 hover:text-white transition">
            Terms &amp; Conditions
          </Link>
          <Link href="/privacy" className="text-gray-400 hover:text-white transition">
            Privacy Policy
          </Link>
        </div>

        {/* Copyright */}
        <p className="text-xs text-gray-500">
          Copyright &copy; 2026 Nexmuse, LLC &mdash; All Rights Reserved
        </p>
      </div>
    </footer>
  );
}
