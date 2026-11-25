import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import apiClient from "../api/client";
import BixLogo from "./BixLogo";
import { useAuth } from "../context/AuthContext";

// 16px outline icons, 1.5px stroke, currentColor
const Icon = ({ d }) => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.5"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    className="shrink-0"
  >
    {d.map((p) => (
      <path key={p} d={p} />
    ))}
  </svg>
);

const ICONS = {
  dashboard: ["M3 3h7v9H3z", "M14 3h7v5h-7z", "M14 12h7v9h-7z", "M3 16h7v5H3z"],
  scrubber: ["M9 11l3 3 8-8", "M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9"],
  monitor: ["M4 4h16v12H4z", "M8 20h8", "M12 16v4", "M7 10h2l1.5-3 3 6 1.5-3h2"],
  docreview: ["M6 3h9l4 4v14H6z", "M14 3v5h5", "M9 14l2 2 4-4"],
  denial: ["M12 3l9 16H3z", "M12 10v4", "M12 17h.01"],
  resub: ["M4 4v5h5", "M20 20v-5h-5", "M5.5 15a7 7 0 0 0 12.9 1.5", "M18.5 9A7 7 0 0 0 5.6 7.5"],
  analytics: ["M4 20V10", "M10 20V4", "M16 20v-7", "M3 20h18"],
  packs: ["M6 3h9l4 4v14H6z", "M14 3v5h5", "M9 13h7", "M9 17h5"],
  profiles: ["M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", "M4 21a8 8 0 0 1 16 0"],
  jobs: ["M21 12a9 9 0 1 1-3-6.7", "M21 4v5h-5", "M12 8v4l3 2"],
  settings: ["M4 21v-7", "M4 10V3", "M12 21v-9", "M12 8V3", "M20 21v-5", "M20 12V3", "M2 14h4", "M10 8h4", "M18 16h4"],
  shield: ["M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z", "M9 12l2 2 4-4"],
  users: ["M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", "M2 21a7 7 0 0 1 14 0", "M16 3.1a4 4 0 0 1 0 7.8", "M18 14a6 6 0 0 1 4 7"],
  login: ["M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4", "M10 17l5-5-5-5", "M15 12H3"],
};

// RBAC: who sees which page. Viewer = reports and dashboards only (see the facility access spec).
const WORK = ["admin", "billing_manager", "staff", "group_admin", "facility_admin"];

export const NAV_SECTIONS = [
  {
    label: "Overview",
    items: [
      { to: "/", label: "Dashboard", icon: "dashboard", end: true },
      { to: "/alerts", label: "Facility alerts", icon: "denial", badge: "alerts" },
    ],
  },
  {
    label: "Claim scrubbing & analysis",
    items: [
      { to: "/scrubber", label: "Claim scrubber", icon: "scrubber", roles: WORK },
      { to: "/doc-review", label: "Documentation review", icon: "docreview", roles: WORK },
      { to: "/dha-sync", label: "XML scrub monitor", icon: "monitor", roles: WORK },
      { to: "/calibration", label: "Denial intelligence", icon: "denial", roles: WORK },
      { to: "/denial-trends", label: "Denial trends", icon: "analytics", roles: WORK },
      { to: "/resub-worklist", label: "Resubmission worklist", icon: "resub", roles: WORK },
      { to: "/scrub-rules", label: "Scrub rules & references", icon: "docreview", roles: ["admin"] },
    ],
  },
  {
    label: "Reports & analytics",
    items: [
      { to: "/analytics", label: "Analytics & reports", icon: "analytics" },
      { to: "/turnaround", label: "Payer turnaround", icon: "jobs" },
      { to: "/report-packs", label: "Client report packs", icon: "packs" },
      { to: "/clinical-profiles", label: "Clinical profiles", icon: "profiles", roles: WORK },
    ],
  },
  {
    label: "Data & administration",
    items: [
      { to: "/jobs", label: "Data sync & jobs", icon: "jobs", roles: ["admin", "billing_manager"] },
      { to: "/facility-settings", label: "Facility settings", icon: "settings", roles: ["admin"] },
      { to: "/users", label: "Users", icon: "users", roles: ["admin"] },
      { to: "/login-activity", label: "Login activity", icon: "login", roles: ["admin", "billing_manager"] },
      { to: "/account-security", label: "Authenticator app", icon: "shield", roles: WORK },
    ],
  },
];

// May this user open this path? (pages not in the menu are open to everyone signed in)
// user.pages (from /auth/me) is the Super Admin's menu-by-role setting; null = everything (Super Admin).
// Admin-only items (roles: ["admin"...]) keep their fixed roles.
export function itemAllowed(item, user) {
  if (!user) return false;
  if (user.role === "admin") return true;
  const adminOnly = item.roles && !item.roles.includes("staff") && !item.roles.includes("viewer");
  if (adminOnly) return item.roles.includes(user.role);
  if (Array.isArray(user.pages)) return user.pages.includes(item.to);
  return !item.roles || item.roles.includes(user.role);
}

export function canOpen(pathname, user) {
  const hit = findNavItem(pathname);
  return !hit || itemAllowed(hit.item, user);
}

// Section + item for a pathname (exact match for "/", prefix match otherwise)
export function findNavItem(pathname) {
  for (const section of NAV_SECTIONS) {
    for (const item of section.items) {
      const match = item.end
        ? pathname === item.to
        : pathname === item.to || pathname.startsWith(item.to + "/");
      if (match) return { section, item };
    }
  }
  return null;
}

const linkClasses = ({ isActive }) =>
  `flex h-9 items-center gap-2.5 rounded-lg px-3 text-[13.5px] transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-ghaf-600 ${
    isActive ? "bg-sage-mint font-semibold text-ghaf-600" : "text-ink hover:bg-sage-mint"
  }`;

export default function Sidebar({ open = false, onClose }) {
  const { user } = useAuth();
  const allowed = (item) => itemAllowed(item, user);
  const [alertCount, setAlertCount] = useState(0);
  useEffect(() => {
    let alive = true;
    const load = () => apiClient.get("/alerts").then(r => alive && setAlertCount(r.data.count || 0)).catch(() => {});
    load();
    const t = setInterval(load, 30 * 60 * 1000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  return (
    <>
      {open && (
        <div
          className="fixed inset-0 z-30 bg-ink-deep/40 lg:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}
      <aside
        id="main-sidebar"
        className={`fixed inset-y-0 left-0 z-40 flex h-full w-60 shrink-0 flex-col bg-sage px-3 py-6 transition-transform lg:static lg:translate-x-0 ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="mb-6 px-3">
          <BixLogo caption="Business intelligence · by Ghaf" />
        </div>
        <nav className="flex flex-1 flex-col gap-5 overflow-y-auto" aria-label="Main">
          {NAV_SECTIONS.map((section) => (
            <div key={section.label}>
              <p className="mb-1 px-3 text-[10.5px] font-semibold uppercase tracking-[0.14em] text-slate-600">
                {section.label}
              </p>
              <div className="flex flex-col gap-0.5">
                {section.items.filter(allowed).map((item) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    end={item.end}
                    className={linkClasses}
                    onClick={onClose}
                  >
                    <Icon d={ICONS[item.icon]} />
                    {item.label}
                    {item.badge === "alerts" && alertCount > 0 && (
                      <span className="ml-auto rounded-full bg-denied px-1.5 text-[11px] font-semibold leading-5 text-white"
                        aria-label={`${alertCount} alerts`}>{alertCount}</span>
                    )}
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>
        <p className="mt-4 px-3 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-600">
          Rooted in healthcare
        </p>
      </aside>
    </>
  );
}
