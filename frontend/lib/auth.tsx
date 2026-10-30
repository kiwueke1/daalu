"use client";

import { createContext, useContext, useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { api, type CurrentUser, UnauthorizedError } from "@/lib/api";

interface AuthContextValue {
  user: CurrentUser | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(true);
  const pathname = usePathname();

  async function refresh() {
    try {
      const me = await api.auth.me();
      setUser(me);
    } catch (err) {
      setUser(null);
      // /api.ts already redirects on 401; we just clear local state.
      if (!(err instanceof UnauthorizedError)) {
        console.error("auth.refresh", err);
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // The login + accept-invite pages handle their own auth state.
    // accept-invite is opened by users without an account yet, so
    // probing /auth/me there would 401 → bounce to /login → break
    // the redemption flow.
    if (
      pathname === "/login"
      || pathname.startsWith("/accept-invite")
      || pathname === "/signup"
      || pathname.startsWith("/verify-email")
    ) {
      setLoading(false);
      return;
    }
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname]);

  async function login(email: string, password: string) {
    const res = await api.auth.login(email, password);
    setUser(res.user);
  }

  async function logout() {
    setUser(null);
    // Full-page navigation to the backend logout so it can clear the
    // session cookie and, in SSO mode, end the Keycloak session
    // (RP-initiated logout) before landing back on /login. Works in
    // password mode too (it just clears the cookie and redirects).
    window.location.href = "/api/v1/auth/oidc/logout";
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, refresh }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
