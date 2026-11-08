import useAnalyticsReport from "../hooks/useAnalyticsReport";
import DataTable from "../components/DataTable";
import { currency, number, percent } from "../utils/format";

// Dashboard table tabs: the breakdowns that used to sit as charts at the foot of Analytics & reports.
// They use the Analytics reports, filtered by encounter date like the claim cycle.
export const TABLE_TABS = [
  { key: "payers", label: "Payers" },
  { key: "clinicians", label: "Clinicians" },
  { key: "departments", label: "Departments" },
  { key: "denials", label: "Denials" },
  { key: "services", label: "Services" },
];

const ENDPOINT = { payers: "insurance", clinicians: "clinicians", departments: "department", denials: "denials", services: "submissions" };
const TOP = 25;

function Block({ title, hint, children }) {
  return (
    <div className="gh-card p-5">
      <p className="gh-label">{title}</p>
      {hint && <p className="mb-3 text-xs text-slate-500">{hint}</p>}
      <div className={hint ? "" : "mt-3"}>{children}</div>
    </div>
  );
}

const top = (rows, key) => [...(rows || [])].sort((a, b) => Number(b[key] || 0) - Number(a[key] || 0)).slice(0, TOP);
const hintTop = (rows) => ((rows || []).length > TOP ? `Top ${TOP} of ${rows.length} by amount` : null);

export default function DashboardTables({ tab, facilityId, range }) {
  const filters = { date_from: range.from, date_to: range.to, ...(facilityId ? { facility_id: facilityId } : {}) };
  const { data, error, loading } = useAnalyticsReport(ENDPOINT[tab], filters);

  if (loading) {
    return (
      <div className="gh-card p-10 text-center text-sm text-slate-500">
        Loading… {facilityId ? "" : "All facilities can take up to 20 seconds the first time."}
      </div>
    );
  }
  if (error) return <div className="gh-card border-red-200 bg-red-50 p-4 text-sm text-red-700">{error}</div>;
  if (!data) return null;

  const note = <p className="text-xs text-slate-500">Claims with an encounter date from {range.from} to {range.to}.</p>;

  if (tab === "payers") {
    return (
      <div className="space-y-4">
        {note}
        <Block title="By receiver (TPA)" hint={hintTop(data.by_receiver)}>
          <DataTable rows={top(data.by_receiver, "claimed_amount")} columns={[
            { key: "receiver", label: "Receiver" },
            { key: "claim_count", label: "Claims", render: number },
            { key: "claimed_amount", label: "Claimed", render: currency },
          ]} />
        </Block>
        <Block title="By payer" hint={hintTop(data.by_payer)}>
          <DataTable rows={top(data.by_payer, "claimed_amount")} columns={[
            { key: "payer", label: "Payer" },
            { key: "claim_count", label: "Claims", render: number },
            { key: "claimed_amount", label: "Claimed", render: currency },
            { key: "paid_amount", label: "Paid", render: currency },
            { key: "denied_amount", label: "Denied", render: currency },
            { key: "pct_of_total", label: "% of total", render: percent },
          ]} />
        </Block>
      </div>
    );
  }

  if (tab === "clinicians") {
    return (
      <div className="space-y-4">
        {note}
        <Block title={`Clinicians — ${number(data.kpis?.clinician_count)}`} hint={hintTop(data.clinicians)}>
          <DataTable rows={top(data.clinicians, "claimed_amount")} columns={[
            { key: "clinician", label: "Clinician" },
            { key: "department", label: "Department" },
            { key: "claim_count", label: "Claims", render: number },
            { key: "claimed_amount", label: "Claimed", render: currency },
            { key: "paid_amount", label: "Paid", render: currency },
            { key: "denied_amount", label: "Denied", render: currency },
            { key: "pct_of_total", label: "% of total", render: percent },
          ]} />
        </Block>
      </div>
    );
  }

  if (tab === "departments") {
    return (
      <div className="space-y-4">
        {note}
        <Block title="Departments" hint={hintTop(data.by_department)}>
          <DataTable rows={top(data.by_department, "claimed_amount")} columns={[
            { key: "department", label: "Department" },
            { key: "claim_count", label: "Claims", render: number },
            { key: "claimed_amount", label: "Claimed", render: currency },
            { key: "paid_amount", label: "Paid", render: currency },
            { key: "denied_amount", label: "Denied", render: currency },
            { key: "pct_of_total", label: "% of total", render: percent },
          ]} />
        </Block>
      </div>
    );
  }

  if (tab === "denials") {
    return (
      <div className="space-y-4">
        {note}
        <Block title="By denial code" hint={hintTop(data.by_code)}>
          <DataTable rows={top(data.by_code, "amount")} columns={[
            { key: "code", label: "Code" },
            { key: "description", label: "Description" },
            { key: "count", label: "Count", render: number },
            { key: "amount", label: "Amount", render: currency },
          ]} />
        </Block>
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <Block title="By payer" hint={hintTop(data.by_payer)}>
            <DataTable rows={top(data.by_payer, "amount")} columns={[
              { key: "payer", label: "Payer" },
              { key: "amount", label: "Amount", render: currency },
            ]} />
          </Block>
          <Block title="By clinician" hint={hintTop(data.by_clinician)}>
            <DataTable rows={top(data.by_clinician, "amount")} columns={[
              { key: "clinician", label: "Clinician" },
              { key: "amount", label: "Amount", render: currency },
            ]} />
          </Block>
        </div>
      </div>
    );
  }

  // services
  return (
    <div className="space-y-4">
      {note}
      <Block title="Top services" hint={hintTop(data.by_service)}>
        <DataTable rows={top(data.by_service, "claimed_amount")} columns={[
          { key: "code", label: "Code" },
          { key: "description", label: "Description" },
          { key: "claimed_amount", label: "Claimed", render: currency },
        ]} />
      </Block>
    </div>
  );
}
