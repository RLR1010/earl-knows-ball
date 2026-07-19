"use client";

import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  type ReactNode,
} from "react";
import { api, type UserProfile } from "./api";

interface AuthContextValue {
  user: UserProfile | null;
  loading: boolean;
  sendCode: (email: string) => Promise<{ message: string }>;
  verifyCode: (email: string, code: string) => Promise<UserProfile>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const profile = await api.auth.me();
      setUser(profile);
    } catch {
      // Not logged in — that's fine
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const sendCode = useCallback(async (email: string) => {
    const result = await api.auth.sendCode(email);
    return result;
  }, []);

  const verifyCode = useCallback(async (email: string, code: string) => {
    const result = await api.auth.verifyCode(email, code);
    setUser(result.user);
    // Save token to localStorage for backward compat (admin pages, etc.)
    if (result.token) {
      localStorage.setItem("earl_token", result.token);
    }
    return result.user;
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.auth.logout();
    } catch {
      // Even if the API call fails, clear local state
    }
    localStorage.removeItem("earl_token");
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, sendCode, verifyCode, logout, refresh }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
