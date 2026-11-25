import { useEffect } from "react";
import { useAuth } from "../context/AuthContext";

// RBAC: only a Super Admin can view "All facilities". Everyone else gets their own facilities from
// GET /facilities; when the current choice is empty or not one of them, pick the first.
export function useIsSuperAdmin() {
  const { user } = useAuth();
  return user?.role === "admin";
}

export default function useFacilityDefault(facilities, value, setValue) {
  const isSuper = useIsSuperAdmin();
  useEffect(() => {
    if (isSuper || !facilities?.length) return;
    if (!facilities.some((f) => String(f.id) === String(value))) setValue(typeof value === "number" ? facilities[0].id : String(facilities[0].id));
  }, [isSuper, facilities, value, setValue]);
  return isSuper;
}
