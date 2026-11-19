import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, ApiError, SESSION_EXPIRED_EVENT, type User } from "./api";

type AuthState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "signed-out" }
  | { status: "signed-in"; user: User };

interface AuthContextValue {
  state: AuthState;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
  retry: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });

  const load = useCallback(async () => {
    setState({ status: "loading" });
    try {
      const { user } = await api<{ user: User }>("/auth/me");
      setState({ status: "signed-in", user });
    } catch (err) {
      const signedOut = err instanceof ApiError && err.status === 401;
      setState({ status: signedOut ? "signed-out" : "error" });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const onExpired = () => setState({ status: "signed-out" });
    window.addEventListener(SESSION_EXPIRED_EVENT, onExpired);
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, onExpired);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      state,
      retry: () => void load(),
      signIn: async (email, password) => {
        const { user } = await api<{ user: User }>("/auth/login", {
          method: "POST",
          body: { email, password },
        });
        setState({ status: "signed-in", user });
      },
      signOut: async () => {
        await api("/auth/logout", { method: "POST" });
        setState({ status: "signed-out" });
      },
    }),
    [state, load],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
