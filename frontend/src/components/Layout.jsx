import { useEffect, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import Sidebar, { findNavItem } from "./Sidebar";
import { useAuth } from "../context/AuthContext";

export default function Layout() {
  const { user, logout } = useAuth();
  const { pathname } = useLocation();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const current = findNavItem(pathname);

  // Close the overlay sidebar on navigation
  useEffect(() => setSidebarOpen(false), [pathname]);

  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex items-center justify-between gap-4 border-b border-hairline bg-white px-4 py-3 lg:px-8">
          <div className="flex min-w-0 items-center gap-3">
            <button
              type="button"
              onClick={() => setSidebarOpen((o) => !o)}
              className="gh-btn-secondary px-2 py-1.5 lg:hidden"
              aria-label="Toggle navigation"
              aria-controls="main-sidebar"
              aria-expanded={sidebarOpen}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true">
                <path d="M4 6h16M4 12h16M4 18h16" />
              </svg>
            </button>
            <nav aria-label="Breadcrumb" className="min-w-0 truncate text-sm">
              {current ? (
                <>
                  <span className="text-slate-500">{current.section.label}</span>
                  <span className="mx-2 text-slate-400" aria-hidden="true">/</span>
                  <span className="font-semibold text-slate-800" aria-current="page">
                    {current.item.label}
                  </span>
                </>
              ) : (
                <span className="gh-eyebrow">Revenue cycle · by Ghaf</span>
              )}
            </nav>
          </div>
          <div className="flex shrink-0 items-center gap-4">
            <div className="hidden text-right sm:block">
              <p className="text-sm font-semibold text-slate-800">{user?.full_name}</p>
              <p className="text-xs capitalize text-slate-500">{user?.role?.replace("_", " ")}</p>
            </div>
            <button onClick={logout} className="gh-btn-secondary py-1.5">
              Sign out
            </button>
          </div>
        </header>
        <main className="flex-1 overflow-y-auto bg-white px-4 py-8 lg:px-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
