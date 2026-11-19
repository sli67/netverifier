import { Link, NavLink, Route, Routes, useLocation } from "react-router";
import type { User } from "./api";
import { useAuth } from "./auth";
import { Icon } from "./Icon";
import { Login } from "./Login";
import { InvestigationDetail } from "./pages/InvestigationDetail";
import { InvestigationList } from "./pages/InvestigationList";
import { SnapshotDetail } from "./pages/SnapshotDetail";
import { SnapshotList } from "./pages/SnapshotList";
import { SnapshotNew } from "./pages/SnapshotNew";

export function App() {
  const { state, retry } = useAuth();

  switch (state.status) {
    case "loading":
      return (
        <main className="center">
          <p className="muted">Loading…</p>
        </main>
      );
    case "error":
      return (
        <main className="center">
          <div className="card">
            <h1>Can't reach the server</h1>
            <p className="muted">Check that the API is running, then try again.</p>
            <div>
              <button onClick={retry}>Try again</button>
            </div>
          </div>
        </main>
      );
    case "signed-out":
      return <Login />;
    case "signed-in":
      return <Shell user={state.user} />;
  }
}

function Shell({ user }: { user: User }) {
  const { signOut } = useAuth();
  const location = useLocation();
  const investigating = location.pathname.startsWith("/investigations/");

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <aside className="sidebar">
        <Link className="brand" to="/" aria-label="Verifier home">
          <span className="brand-mark"><Icon name="network" size={22} /></span>
          <span>verifier<span className="brand-caption">NETWORK CHANGE REVIEW</span></span>
        </Link>
        <div className="sidebar-section-label">WORKSPACE</div>
        <nav className="sidebar-nav" aria-label="Main navigation">
          <NavLink to="/" end className={({ isActive }) => isActive || (location.pathname.startsWith("/snapshots/") && location.pathname !== "/snapshots/new") ? "nav-item active" : "nav-item"}><Icon name="layers" />Snapshots</NavLink>
          <NavLink to="/investigations" end className={({ isActive }) => isActive ? "nav-item active" : "nav-item"}><Icon name="search" />Investigations</NavLink>
          {investigating && <div className="nav-item active" aria-current="page"><Icon name="spark" />Agent workspace</div>}
          <NavLink to="/snapshots/new" className={({ isActive }) => isActive ? "nav-item active" : "nav-item"}><Icon name="plus" />Upload snapshot</NavLink>
        </nav>
        <div className="sidebar-note">
          <Icon name="shield" size={22} />
          <strong>Explore with confidence</strong>
          <p>Your agent can inspect configurations and query Batfish. Your network stays untouched.</p>
        </div>
        <div className="sidebar-user">
          <span className="user-avatar">{user.email.charAt(0).toUpperCase()}</span>
          <div className="user-info"><strong title={user.email}>{user.email}</strong><span>{user.role}</span></div>
          <button className="icon-button" onClick={() => void signOut()} aria-label="Sign out" title="Sign out"><Icon name="logout" size={18} /></button>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb"><span>Workspace</span><Icon name="chevron" size={14} /><strong>{investigating ? "Agent investigation" : location.pathname === "/investigations" ? "Investigations" : "Snapshots"}</strong></div>
          <span className="environment"><span className="status-dot" />Configuration analysis</span>
        </header>
        <main id="main-content" className={investigating ? "content investigation-content" : "content"}>
        <Routes>
          <Route path="/" element={<SnapshotList />} />
          <Route path="/snapshots/new" element={<SnapshotNew />} />
          <Route path="/snapshots/:id" element={<SnapshotDetail />} />
          <Route path="/investigations" element={<InvestigationList />} />
          <Route path="/investigations/:id" element={<InvestigationDetail />} />
          <Route
            path="*"
            element={
              <div className="card">
                <h2>Page not found</h2>
                <div>
                  <Link className="button secondary" to="/">
                    Back to snapshots
                  </Link>
                </div>
              </div>
            }
          />
        </Routes>
        </main>
      </div>
    </div>
  );
}
