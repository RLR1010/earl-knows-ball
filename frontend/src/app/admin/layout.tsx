"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";

const NAV_ITEMS = [
  { label: "Dashboard", path: "/admin", icon: "📊" },
  { label: "Users", path: "/admin/users", icon: "👥" },
  { label: "Subscriptions", path: "/admin/subscriptions", icon: "🔄" },
  { label: "Plans", path: "/admin/plans", icon: "📋" },
  { label: "Models", path: "/admin/models", icon: "🧠" },
  { label: "Articles", path: "/admin/articles", icon: "📰" },
  { label: "　RSS Feeds", path: "/admin/articles/rss", icon: "📡" },
  { label: "Predictions", path: "/admin/predictions", icon: "🎯" },
  { label: "Tasks", path: "/admin/tasks", icon: "⚡" },
  { label: "Data Loader", path: "/admin/data-loader", icon: "🧪" },
  { label: "Database", path: "/admin/database", icon: "🗄️" },
  { label: "Structure", path: "/admin/structure", icon: "🏗️" },
];

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const [isAdmin, setIsAdmin] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(true);
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    const checkAdmin = async () => {
      const token = localStorage.getItem("earl_token");
      if (!token) {
        setIsAdmin(false);
        setLoading(false);
        return;
      }

      try {
        const res = await fetch("/auth/me", {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!res.ok) throw new Error("Not authorized");
        const user = await res.json();
        if (!user.is_admin) {
          setIsAdmin(false);
        } else {
          setIsAdmin(true);
        }
      } catch {
        setIsAdmin(false);
      } finally {
        setLoading(false);
      }
    };
    checkAdmin();
  }, []);

  useEffect(() => {
    if (!loading && !isAdmin) {
      router.push("/login");
    }
  }, [loading, isAdmin, router]);

  if (loading) {
    return (
      <div className="min-h-screen bg-[#0a0a0f] flex items-center justify-center">
        <div className="text-gray-400 text-lg">Loading...</div>
      </div>
    );
  }

  if (!isAdmin) {
    return null;
  }

  return (
    <div className="min-h-screen bg-[#0a0a0f]">
      <div className="flex">
        {/* Sidebar */}
        <aside className="w-64 min-h-screen bg-black/40 border-r border-white/10 fixed left-0 top-0 z-40">
          <div className="p-6">
            <a href="/admin" className="font-display text-xl font-bold text-earl-400 tracking-wide">
              ⚡ ADMIN
            </a>
          </div>
          <nav className="px-3 space-y-1">
            {NAV_ITEMS.map((item) => {
              const isActive = pathname === item.path || (item.path !== "/admin" && pathname.startsWith(item.path + "/"));
              return (
                <a
                  key={item.path}
                  href={item.path}
                  className={`flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm font-medium transition ${
                    isActive
                      ? "bg-earl-600/20 text-earl-400 border border-earl-600/30"
                      : "text-gray-400 hover:text-white hover:bg-white/5"
                  }`}
                >
                  <span className="text-base">{item.icon}</span>
                  {item.label}
                </a>
              );
            })}
          </nav>
          <div className="absolute bottom-6 left-0 right-0 px-6">
            <a
              href="/"
              className="flex items-center gap-2 text-xs text-gray-500 hover:text-gray-300 transition"
            >
              ← Back to site
            </a>
          </div>
        </aside>

        {/* Main content */}
        <main className="ml-64 flex-1 min-h-screen p-8">
          {children}
        </main>
      </div>
    </div>
  );
}
