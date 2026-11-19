import { useState, type FormEvent } from "react";
import { Icon } from "./Icon";
import { ApiError } from "./api";
import { useAuth } from "./auth";

function messageFor(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === "invalid_credentials") return "Incorrect email or password.";
    if (err.code === "too_many_attempts") return "Too many attempts. Try again in a few minutes.";
  }
  return "Couldn't reach the server. Check that the API is running and try again.";
}

export function Login() {
  const { signIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(email, password);
    } catch (err) {
      setError(messageFor(err));
      setBusy(false);
    }
  }

  return (
    <main className="login-page">
      <div className="login-story">
        <div className="brand"><span className="brand-mark"><Icon name="network" size={24} /></span>verifier</div>
        <div><p className="eyebrow">CLARITY BEFORE CHANGE</p><h2>Understand your network.<br />Make your next move<br /><span>with confidence.</span></h2><p>Turn configurations into conversations. Investigate routing, explore policies, and review your network with an agent by your side.</p></div>
        <div className="login-assurance"><Icon name="shield" size={19} /> Read-only investigation. Evidence-backed answers.</div>
      </div>
      <div className="login-form-area">
      <form className="card login" onSubmit={onSubmit}>
        <span className="login-symbol"><Icon name="spark" size={26} /></span><h1>Welcome back</h1>
        <p className="muted">Sign in to your network workspace.</p>

        <label htmlFor="email">Email</label>
        <input
          id="email"
          type="email"
          autoComplete="username"
          autoFocus
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />

        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />

        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}

        <button type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      </div>
    </main>
  );
}
