"use client";

import { useEffect } from "react";
import { useRouter, usePathname } from "next/navigation";
import { useAuth } from "@/lib/auth-context";

const NAV_GROUPS = [
  {
    label: "Site Admin",
    items: [
      { label: "Users", path: "/admin/users" },
      { label: "Subscriptions", path: "/admin/subscriptions" },
      { label: "Plans", path: "/admin/plans" },
      { label: "Payments", path: "/admin/payments" },
    ],
  },
  {
    label: "Site Content",
    items: [
      { label: "Game Previews", path: "/admin/content" },
      { label: "Articles", path: "/admin/articles" },
      { label: "RSS Feeds", path: "/admin/articles/rss" },
    ],
  },
  {
    label: "Machine Learning",
    items: [
      { label: "Models", path: "/admin/models" },
      { label: "Predictions", path: "/admin/predictions" },
    ],
  },
  {
    label: "System",
    items: [
      { label: "Tasks", path: "/admin/tasks" },
      { label: "Data Loader", path: "/admin/data-loader" },
      { label: "Database", path: "/admin/database" },
      { label: "Structure", path: "/admin/structure" },
    ],
  },
];

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  const isAdmin = user?.is_admin === true;

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
        <aside className="w-64 h-screen bg-black/40 border-r border-white/10 fixed left-0 top-[80px] z-40 flex flex-col">
          {/* Home icon: first row of the sidebar, above the menus */}
          <div className="flex-none px-5 pt-5 pb-3">
            <a
              href="/admin"
              title="Dashboard"
              aria-label="Dashboard"
              className="inline-flex items-center justify-center w-10 h-10 rounded-lg text-white hover:bg-white/10 transition"
            >
              <svg xmlns="http://www.w3.org/2000/svg" className="w-7 h-7" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M3 12l9-9 9 9M5 10v10a1 1 0 001 1h3a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1h3a1 1 0 001-1V10" />
              </svg>
            </a>
          </div>

          <nav className="flex-1 overflow-y-auto px-3 space-y-1">
            {NAV_GROUPS.map((group) => {
              const isGroupActive = group.items.some(
                (item) =>
                  pathname === item.path ||
                  (item.path !== "/admin" && pathname.startsWith(item.path + "/"))
              );
              return (
                <div key={group.label}>
                  <div
                    className={`px-4 pt-4 pb-1 text-sm font-bold uppercase tracking-wide ${
                      isGroupActive ? "text-earl-400" : "text-earl-400/80"
                    }`}
                  >
                    {group.label}
                  </div>
                  <div className="space-y-1">
                    {group.items.map((item) => {
                      const isActive =
                        pathname === item.path ||
                        (item.path !== "/admin" && pathname.startsWith(item.path + "/"));
                      return (
                        <a
                          key={item.path}
                          href={item.path}
                          className={`flex items-center px-4 py-1.5 rounded-lg text-sm font-medium transition ${
                            isActive
                              ? "bg-earl-600/20 text-earl-400 border border-earl-600/30"
                              : "text-gray-400 hover:text-white hover:bg-white/5"
                          }`}
                        >
                          {item.label}
                        </a>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </nav>

          <div className="p-6">
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
