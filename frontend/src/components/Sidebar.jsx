import { NavLink } from "react-router-dom";
import BixLogo from "./BixLogo";

const navItems = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/clinical-profiles", label: "Clinical Profiles" },
  { to: "/dha-sync", label: "XML Scrub Monitor" },
  { to: "/analytics", label: "Analytics & Financial Insights" },
  { to: "/facility-settings", label: "Facility Settings" },
  { to: "/login-activity", label: "Login Activity" },
  { to: "/jobs", label: "Background Jobs" },
  { to: "/scrubber", label: "Claim Scrubber" },
  { to: "/calibration", label: "Denial Intelligence" },
  { to: "/resub-worklist", label: "Resubmission Worklist" },
];

const linkClasses = ({ isActive }) =>
  `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-ghaf-600 ${
    isActive ? "bg-sage-mint font-semibold text-ghaf-600" : "text-ink hover:bg-sage-mint"
  }`;

export default function Sidebar() {
  return (
    <aside className="flex h-full w-64 flex-col bg-sage px-3 py-6">
      <div className="mb-8 px-3">
        <BixLogo caption="Business intelligence · by Ghaf" />
      </div>
      <nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto" aria-label="Main">
        {navItems.map((item, i) => (
          <NavLink key={item.to} to={item.to} end={item.end} className={linkClasses}>
            <span className="w-5 text-[10px] font-semibold tracking-[0.1em] text-ghaf-600">
              {String(i + 1).padStart(2, "0")}
            </span>
            {item.label}
          </NavLink>
        ))}
      </nav>
      <p className="px-3 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-600">
        Rooted in healthcare
      </p>
    </aside>
  );
}
