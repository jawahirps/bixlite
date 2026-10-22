export const currency = (value) =>
  new Intl.NumberFormat("en-AE", { style: "currency", currency: "AED", maximumFractionDigits: 0 }).format(
    Number(value || 0)
  );

export const number = (value) => new Intl.NumberFormat("en-US").format(Number(value || 0));

export const percent = (value) => `${Number(value || 0).toFixed(1)}%`;

// Today's date as YYYY-MM-DD in the browser's local time zone (toISOString() is UTC,
// which in the UAE gives yesterday's date before 04:00).
export const localISODate = (d = new Date()) => {
  const z = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}`;
};
