import { useEffect } from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";
import apiClient from "../api/client";
import { useAuth } from "../context/AuthContext";
import { canOpen } from "./Sidebar";

export default function ProtectedRoute() {
  const { isAuthenticated, user, updateUser } = useAuth();
  const { pathname } = useLocation();

  // Refresh the signed-in user once per load: picks up role / facility changes made by a Super Admin
  useEffect(() => {
    if (isAuthenticated) apiClient.get("/auth/me").then((r) => updateUser(r.data)).catch(() => {});
  }, [isAuthenticated, updateUser]);

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }
  if (user && !canOpen(pathname, user)) {
    return <Navigate to="/" replace />;
  }

  return <Outlet />;
}
