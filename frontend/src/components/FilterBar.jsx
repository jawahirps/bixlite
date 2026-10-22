import { useEffect, useRef, useState } from "react";
import apiClient from "../api/client";

const selectClass = "gh-input w-full";

const labelClass = "gh-label mb-1.5 block";

// ── Multi-select facility dropdown ────────────────────────────────────────
function FacilityMultiSelect({ facilities, selectedIds, onChange }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  // Close on outside click
  useEffect(() => {
    function handle(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("mousedown", handle);
    return () => document.removeEventListener("mousedown", handle);
  }, []);

  const toggle = (id) => {
    const next = selectedIds.includes(id)
      ? selectedIds.filter((x) => x !== id)
      : [...selectedIds, id];
    onChange(next);
  };

  const selectAll = () => onChange([]);
  const label =
    selectedIds.length === 0
      ? "All Facilities"
      : selectedIds.length === 1
      ? facilities.find((f) => f.id === selectedIds[0])?.name || "1 facility"
      : `${selectedIds.length} facilities`;

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="gh-input w-full flex items-center justify-between gap-2"
      >
        <span className={selectedIds.length === 0 ? "text-slate-400" : "text-slate-700"}>
          {label}
        </span>
        <svg
          xmlns="http://www.w3.org/2000/svg"
          className={`h-4 w-4 text-slate-400 transition-transform ${open ? "rotate-180" : ""}`}
          viewBox="0 0 20 20" fill="currentColor"
        >
          <path fillRule="evenodd" d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z" clipRule="evenodd" />
        </svg>
      </button>

      {open && (
        <div className="absolute z-30 mt-1 w-full min-w-[260px] rounded-lg border border-hairline bg-white shadow-[0_8px_24px_rgba(11,34,51,0.10)]">
          {/* All Facilities option */}
          <button
            type="button"
            onClick={selectAll}
            className={`w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-slate-50 border-b border-slate-100 ${selectedIds.length === 0 ? "font-semibold text-ghaf-700" : "text-slate-600"}`}
          >
            <span className={`w-4 h-4 rounded border flex items-center justify-center flex-shrink-0 ${selectedIds.length === 0 ? "bg-ghaf-600 border-ghaf-600" : "border-slate-300"}`}>
              {selectedIds.length === 0 && (
                <svg className="w-3 h-3 text-white" viewBox="0 0 12 12" fill="none">
                  <path d="M2 6l3 3 5-5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
              )}
            </span>
            All Facilities
          </button>

          {/* Individual facilities */}
          <div className="max-h-64 overflow-y-auto py-1">
            {facilities.map((f) => {
              const checked = selectedIds.includes(f.id);
              return (
                <button
                  key={f.id}
                  type="button"
                  onClick={() => toggle(f.id)}
                  className="w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-slate-50 text-left"
                >
                  <span className={`w-4 h-4 rounded border flex items-center justify-center flex-shrink-0 ${checked ? "bg-ghaf-600 border-ghaf-600" : "border-slate-300"}`}>
                    {checked && (
                      <svg className="w-3 h-3 text-white" viewBox="0 0 12 12" fill="none">
                        <path d="M2 6l3 3 5-5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                      </svg>
                    )}
                  </span>
                  <span className={checked ? "text-slate-900 font-medium" : "text-slate-600"}>
                    {f.name}
                  </span>
                </button>
              );
            })}
          </div>

          {selectedIds.length > 0 && (
            <div className="border-t border-slate-100 px-3 py-2 flex justify-between items-center">
              <span className="text-xs text-slate-600">{selectedIds.length} selected</span>
              <button
                type="button"
                onClick={selectAll}
                className="text-xs text-ghaf-600 hover:underline"
              >
                Clear
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Main FilterBar ─────────────────────────────────────────────────────────
export default function FilterBar({ filters, onChange }) {
  const [options, setOptions] = useState({
    facilities: [],
    receivers: [],
    payers: [],
    encounter_types: [],
    departments: [],
  });

  useEffect(() => {
    apiClient
      .get("/analytics/filters")
      .then((res) => setOptions(res.data))
      .catch(() => {});
  }, []);

  const update = (key, value) => {
    onChange({ ...filters, [key]: value || undefined });
  };

  // facility_ids is stored as an array of ints in state, sent as comma-string to API
  const selectedFacilityIds = filters.facility_ids
    ? filters.facility_ids.split(",").map(Number).filter(Boolean)
    : [];

  const handleFacilityChange = (ids) => {
    if (ids.length === 0) {
      // Remove both facility keys
      const { facility_id, facility_ids, ...rest } = filters;
      onChange(rest);
    } else {
      const { facility_id, ...rest } = filters;
      onChange({ ...rest, facility_ids: ids.join(",") });
    }
  };

  const reset = () => onChange({});

  return (
    <div className="rounded-lg border border-hairline bg-sage p-5">
      {/* Row 1: Entity filters */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-5">
        <div>
          <label className={labelClass}>Facility</label>
          <FacilityMultiSelect
            facilities={options.facilities}
            selectedIds={selectedFacilityIds}
            onChange={handleFacilityChange}
          />
        </div>

        <div>
          <label className={labelClass}>Receiver</label>
          <select
            className={selectClass}
            value={filters.receiver_id || ""}
            onChange={(e) => update("receiver_id", e.target.value)}
          >
            <option value="">All Receivers</option>
            {options.receivers.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className={labelClass}>Payer</label>
          <select
            className={selectClass}
            value={filters.payer_id || ""}
            onChange={(e) => update("payer_id", e.target.value)}
          >
            <option value="">All Payers</option>
            {options.payers.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className={labelClass}>Encounter Type</label>
          <select
            className={selectClass}
            value={filters.encounter_type || ""}
            onChange={(e) => update("encounter_type", e.target.value)}
          >
            <option value="">All Encounter Types</option>
            {options.encounter_types.map((et) => (
              <option key={et} value={et}>
                {et}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className={labelClass}>Department</label>
          <select
            className={selectClass}
            value={filters.department || ""}
            onChange={(e) => update("department", e.target.value)}
          >
            <option value="">All Departments</option>
            {options.departments.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Row 2: Date filters */}
      <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-3">
        <div>
          <label className={labelClass}>Search Criteria</label>
          <select
            className={selectClass}
            value={filters.date_field || "treatment_date"}
            onChange={(e) => update("date_field", e.target.value)}
          >
            <option value="treatment_date">Treatment Date</option>
            <option value="remittance_date">Remittance Transaction Date</option>
          </select>
        </div>

        <div>
          <label className={labelClass}>
            {filters.date_field === "remittance_date" ? "Remittance Date From" : "Treatment Date From"}
          </label>
          <input
            type="date"
            className={selectClass}
            value={filters.date_from || ""}
            onChange={(e) => update("date_from", e.target.value)}
          />
        </div>

        <div>
          <label className={labelClass}>
            {filters.date_field === "remittance_date" ? "Remittance Date To" : "Treatment Date To"}
          </label>
          <input
            type="date"
            className={selectClass}
            value={filters.date_to || ""}
            onChange={(e) => update("date_to", e.target.value)}
          />
        </div>
      </div>

      <div className="mt-3 flex items-center justify-between">
        <p className="text-xs text-slate-600">
          {filters.date_field === "remittance_date"
            ? "Filtering by remittance transaction date"
            : "Filtering by treatment date"}
          {selectedFacilityIds.length > 0 && (
            <span className="ml-2 text-ghaf-600 font-medium">
              · {selectedFacilityIds.length} {selectedFacilityIds.length === 1 ? "facility" : "facilities"} selected
            </span>
          )}
        </p>
        <button
          type="button"
          onClick={reset}
          className="gh-btn-secondary gh-btn-sm bg-white"
        >
          Clear filters
        </button>
      </div>
    </div>
  );
}
