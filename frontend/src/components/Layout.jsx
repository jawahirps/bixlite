import { Outlet } from "react-router-dom";
import Sidebar from "./Sidebar";
import { useAuth } from "../context/AuthContext";

export default function Layout() {
  const { user, logout } = useAuth();

  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <Sidebar />
      <div className="flex flex-1 flex-col overflow-hidden">
        <header className="flex items-center justify-between border-b border-hairline bg-white px-8 py-3">
          <p className="gh-eyebrow">Revenue cycle · by Ghaf</p>
          <div className="flex items-center gap-4">
            <div className="text-right">
              <p className="text-sm font-semibold text-slate-800">{user?.full_name}</p>
              <p className="text-xs capitalize text-slate-500">{user?.role?.replace("_", " ")}</p>
            </div>
            <button
              onClick={logout}
              className="gh-btn-secondary py-1.5"
            >
              Sign out
            </button>
          </div>
        </header>
        <main className="flex-1 overflow-y-auto bg-white px-8 py-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
