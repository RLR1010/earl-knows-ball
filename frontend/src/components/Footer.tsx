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
          <Link href="/pricing" className="text-gray-400 hover:text-white transition">
            Premium Membership
          </Link>
          <Link href="/terms" className="text-gray-400 hover:text-white transition">
            Terms &amp; Conditions
          </Link>
          <Link href="/privacy" className="text-gray-400 hover:text-white transition">
            Privacy Policy
          </Link>
        </div>

        {/* Disclaimer */}
        <p className="text-xs leading-relaxed text-gray-500 max-w-3xl mx-auto text-center mb-6">
          Earl Knows Ball is for <span className="text-gray-400">entertainment purposes only</span>. All
          predictions, picks, and analysis are opinions and are not guaranteed to be accurate. There is no
          guarantee of winning or profit. If you or someone you know has a gambling problem, call 1-800-GAMBLER
          (1-800-426-2537) or visit <Link href="https://www.ncpgambling.org" className="text-gray-400 hover:text-white underline underline-offset-2 transition">ncpgambling.org</Link>.
        </p>

        {/* Copyright */}
        <p className="text-xs text-gray-500">
          Copyright &copy; 2026 Nexmuse, LLC &mdash; All Rights Reserved
        </p>
      </div>
    </footer>
  );
}
