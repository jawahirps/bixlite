import { createContext, useContext, useState, useCallback } from "react";
import apiClient from "../api/client";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(() => {
    const raw = localStorage.getItem("ghaf_user");
    return raw ? JSON.parse(raw) : null;
  });
  const [token, setToken] = useState(() => localStorage.getItem("ghaf_token"));

  const login = useCallback(async (email, password, otp) => {
    const form = new URLSearchParams();
    form.append("username", email);
    form.append("password", password);
    if (otp) form.append("otp", otp);

    const { data } = await apiClient.post("/auth/login", form, {
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
    });

    localStorage.setItem("ghaf_token", data.access_token);
    localStorage.setItem("ghaf_user", JSON.stringify(data.user));
    setToken(data.access_token);
    setUser(data.user);
    return data.user;
  }, []);

  // Keep the stored user in sync after profile changes (e.g. authenticator on/off)
  const updateUser = useCallback((u) => {
    localStorage.setItem("ghaf_user", JSON.stringify(u));
    setUser(u);
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem("ghaf_token");
    localStorage.removeItem("ghaf_user");
    setToken(null);
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider value={{ user, token, login, logout, updateUser, isAuthenticated: !!token }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
