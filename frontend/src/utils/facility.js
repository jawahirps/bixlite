// One remembered facility for the whole app: picking it on any page preselects it on the others.
// Older per-page keys are read once so nobody loses their last choice.
const KEY = "ghaf_facility_id";
const OLD = ["ghaf_dha_facility_id", "ghaf_jobs_facility_id"];

export function readFacility(fallback = "") {
  try {
    const v = localStorage.getItem(KEY) || OLD.map((k) => localStorage.getItem(k)).find(Boolean);
    return v || fallback;
  } catch {
    return fallback;   // private window / blocked storage
  }
}

export function saveFacility(id) {
  try {
    if (id) localStorage.setItem(KEY, String(id));
  } catch { /* ignore */ }
}
