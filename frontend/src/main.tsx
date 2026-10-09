import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
} from "recharts";
import { Eye, EyeSlash, LayoutDashboard, Radio, Settings, Download, Search, ArrowUpRight, TriangleAlert, ChevronRight, RefreshCw, LogOut, ShieldCheck, CalendarDays, Activity, Check, Close, Bell, Menu , PlatformIcon } from "./icons";
// @ts-ignore: CSS is handled by the bundler and has no TypeScript declarations.
import "bootstrap/dist/css/bootstrap.min.css";
// @ts-ignore: CSS is handled by the bundler and has no TypeScript declarations.
import "./style.css";

type Source = {
  platform: string;
  source: string;
  status: string;
  last_synced_at: string | null;
  error?: string;
};
type Session = { role: string; email: string; name: string };
type Rollup = {
  date: string;
  platform?: string;
  positive_count: number;
  negative_count: number;
  neutral_count: number;
  mixed_count: number;
  total_count: number;
  negativity_index: number;
};
type Post = {
  _id: string;
  platform: string;
  author: string;
  content: string;
  url: string;
  published_at: string;
  engagement: {
    likes: number;
    comments: number;
    shares: number;
    views: number;
  };
  engagement_score: number;
  sentiment: {
    label: string;
    confidence: number;
    reason: string;
    model_used: string;
    language?: "hi" | "en" | "hinglish";
    targets?: string[];
    review_required?: boolean;
  } | null;
  classification_status?: string;
};
type Alert = {
  date: string;
  negative_count: number;
  threshold: number;
  top_negative_posts: Post[];
  platform_breakdown: Record<string, number>;
  notified_channels: string[];
  notification_errors?: Record<string, string>;
};
type Overview = {
  date: string;
  timezone: string;
  sources: Source[];
  today: Rollup | null;
  trend: Rollup[];
  platform_totals: Rollup[];
  pending: number;
  threshold: number;
  partial: boolean;
  sync_running?: boolean;
  schedule?: {
    enabled: boolean;
    frequent: SyncScheduleItem | null;
    social: SyncScheduleItem | null;
  };
  classifier: {
    status: string;
    model: string;
    threshold?: number;
    fallback_model?: string;
    fallback_configured?: boolean;
  };
  alerts: Alert[];
};
type SyncScheduleItem = {
  label: string;
  interval_seconds: number;
  next_run_at: string | null;
};
const names: Record<string, string> = {
  facebook: "Facebook",
  instagram: "Instagram",
  x: "X / Twitter",
  youtube: "YouTube",
  news: "News",
  reddit: "Reddit",
};
const colors = {
  positive: "#2f9278",
  negative: "#ce514e",
  neutral: "#a0adbb",
  mixed: "#c68732",
};
const num = (n: number) => n.toLocaleString("en-IN");
const dateLabel = (d: string) =>
  new Date(d + "T12:00:00").toLocaleDateString("en-IN", {
    day: "numeric",
    month: "short",
  });
const sourceStateLabel = (source: Source) => {
  if (source.status === "disconnected") return "Not connected";
  if (/exhaust/i.test(source.error || "")) return "Quota exhausted";
  if (source.status === "unavailable") return "Data unavailable";
  return "Connected";
};
let bearer = "";
async function api(path: string, options: RequestInit = {}) {
  const response = await fetch("/api" + path, {
    ...options,
    headers: {
      ...(options.body instanceof URLSearchParams
        ? {}
        : { "Content-Type": "application/json" }),
      Authorization: "Bearer " + bearer,
      ...options.headers,
    },
  });
  if (!response.ok) {
    const e = await response
      .json()
      .catch(() => ({ detail: "Service unavailable" }));
    throw new Error(
      typeof e.detail === "string" ? e.detail : "Please check the form values",
    );
  }
  return response;
}
async function json(path: string, options: RequestInit = {}) {
  return (await api(path, options)).json();
}

async function overviewJson(path: string) {
  let lastError: unknown;
  for (const delay of [0, 800, 1800]) {
    if (delay) await new Promise((resolve) => window.setTimeout(resolve, delay));
    try {
      return await json(path);
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError;
}

const GlobalLoadingContext = React.createContext<
  (message?: string) => () => void
>(() => () => undefined);

function GlobalLoader({ message }: { message: string }) {
  return (
    <div className="global-loader" role="status" aria-live="polite" aria-label={message}>
      <div className="global-loader-card">
        <span className="global-loader-spinner" aria-hidden="true" />
        <strong>{message}</strong>
        <span className="global-loader-dots" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
      </div>
    </div>
  );
}

function SyncScheduleProgress({ schedule }: { schedule: NonNullable<Overview["schedule"]> }) {
  const [clock, setClock] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const items = [schedule.frequent, schedule.social].filter(Boolean) as SyncScheduleItem[];
  function timing(item: SyncScheduleItem) {
    if (!item.next_run_at) return { progress: 0, copy: "Schedule unavailable", seconds: 0 };
    const remaining = Math.max(0, Math.ceil((new Date(item.next_run_at).getTime() - clock) / 1000));
    const progress = Math.max(0, Math.min(100, (1 - remaining / item.interval_seconds) * 100));
    if (remaining <= 0) return { progress: 100, copy: "Starting now", seconds: 0 };
    const hours = Math.floor(remaining / 3600);
    const minutes = Math.floor((remaining % 3600) / 60);
    const seconds = remaining % 60;
    const copy = hours
      ? `Next in ${hours}h ${minutes}m`
      : `Next in ${minutes}m ${String(seconds).padStart(2, "0")}s`;
    return { progress, copy, seconds: remaining };
  }

  return (
    <section className="sync-schedule" aria-label="Automatic data collection schedule">
      {items.map((item) => {
        const state = timing(item);
        return (
          <div className="sync-schedule-item" key={item.label}>
            <div className="sync-schedule-copy">
              <span>{item.label}</span>
              <strong>{state.copy}</strong>
            </div>
            <div className="sync-schedule-track" role="progressbar" aria-label={`${item.label} next automatic fetch`}
              aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(state.progress)}>
              <i style={{ width: `${state.progress}%` }} />
            </div>
            <small>{item.interval_seconds === 900 ? "Every 15 minutes" : "Every 4 hours"}</small>
          </div>
        );
      })}
    </section>
  );
}

function App() {
  const [session, setSession] = useState<Session | null>(null),
    [error, setError] = useState(""),
    [showPassword, setShowPassword] = useState(false),
    [loginBusy, setLoginBusy] = useState(false);
  async function login(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setLoginBusy(true);
    setError("");
    const data = new FormData(e.currentTarget);
    try {
      const result = await json("/auth/token", {
        method: "POST",
        body: new URLSearchParams({
          username: String(data.get("email")),
          password: String(data.get("password")),
        }),
      });
      bearer = result.access_token;
      setSession(result);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoginBusy(false);
    }
  }
  if (!session)
    return (
      <div className="login">
        <form className="login-form" onSubmit={login}>
          <div className="login-intro">
            <div className="login-logo">
              <Eye />
              <span>JanNetra</span>
            </div>
            <p>Political conversations, clearly understood.</p>
          </div>
          <h1>Log in to JanNetra</h1>
          <input className="form-control"
            aria-label="Email address"
            placeholder="Email address"
            name="email"
            type="email"
            required
            autoComplete="username"
          />
          <div className="password-field">
            <input className="form-control"
              aria-label="Password"
              placeholder="Password"
              name="password"
              type={showPassword ? "text" : "password"}
              required
              autoComplete="current-password"
            />
            <button
              type="button"
              className="password-toggle"
              aria-label={showPassword ? "Hide password" : "Show password"}
              aria-pressed={showPassword}
              onClick={() => setShowPassword((visible) => !visible)}
            >
              {showPassword ? <EyeSlash size={18} /> : <Eye size={18} />}
            </button>
          </div>
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          <button className="btn btn-primary primary" disabled={loginBusy}>
            {loginBusy ? "Signing in..." : "Log in"}
          </button>
        </form>
      </div>
    );
  return (
    <Workspace
      session={session}
      onSessionChange={setSession}
      logout={() => {
        bearer = "";
        setSession(null);
      }}
    />
  );
}

function Workspace({
  session,
  onSessionChange,
  logout,
}: {
  session: Session;
  onSessionChange: (session: Session) => void;
  logout: () => void;
}) {
  const [view, setView] = useState("overview"),
    [platform, setPlatform] = useState(""),
    [data, setData] = useState<Overview | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [revision, setRevision] = useState(0),
    [report, setReport] = useState(false),
    [detail, setDetail] = useState<Alert | null>(null),
    [mobile, setMobile] = useState(false),
    [loadingTasks, setLoadingTasks] = useState(0),
    [loadingMessage, setLoadingMessage] = useState("Loading…");
  const beginLoading = React.useCallback((message = "Loading…") => {
    let finished = false;
    setLoadingMessage(message);
    setLoadingTasks((count) => count + 1);
    return () => {
      if (finished) return;
      finished = true;
      setLoadingTasks((count) => Math.max(0, count - 1));
    };
  }, []);
  useEffect(() => {
    let active = true;
    // Initial load uses the global overlay. Refresh/status polling stays in-place
    // so a background sync never blocks the dashboard every five seconds.
    const finishLoading = data ? () => undefined : beginLoading("Loading dashboard data…");
    setError("");
    overviewJson("/overview" + (platform ? "?platform=" + platform : ""))
      .then((d) => {
        if (active) setData(d);
      })
      .catch((e) => active && setError(e.message))
      .finally(finishLoading);
    return () => {
      active = false;
      finishLoading();
    };
  }, [platform, revision, beginLoading]);
  useEffect(() => {
    const t = setInterval(() => setRevision((x) => x + 1), 60000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    if (!data?.sync_running) return;
    const t = window.setInterval(() => setRevision((x) => x + 1), 5000);
    return () => window.clearInterval(t);
  }, [data?.sync_running]);
  async function refresh() {
    const finishLoading = beginLoading("Refreshing monitoring data…");
    setBusy(true);
    setError("");
    try {
      if (session.role === "admin") await json("/sync", { method: "POST" });
      setRevision((x) => x + 1);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      finishLoading();
    }
  }
  const syncing = busy || !!data?.sync_running;
  const active = data?.alerts.find((a) => a.date === data.date),
    selectedSource = data?.sources.find((s) => s.platform === platform),
    disconnected = selectedSource?.status === "disconnected";
  const navigate = (v: string) => {
    setView(v);
    setMobile(false);
  };
  const navigation = [
    ["overview", "Overview", LayoutDashboard],
    ["feed", "Conversation", Radio],
    ["alerts", "Alerts", Bell],
    ["settings", "Settings", Settings],
  ] as const;
  return (
    <GlobalLoadingContext.Provider value={beginLoading}>
    <div className="shell">
      {loadingTasks > 0 && <GlobalLoader message={loadingMessage} />}
      <header className={mobile ? "app-navbar open" : "app-navbar"}>
        <div className="navbar-inner">
          <button
            type="button"
            className="navbar-brand"
            aria-label="Go to overview"
            onClick={() => navigate("overview")}
          >
            <span className="navbar-logo"><Eye size={22} /></span>
            <strong>JanNetra</strong>
          </button>
          <button
            className="mobile-toggle"
            aria-label="Toggle navigation"
            aria-expanded={mobile}
            onClick={() => setMobile(!mobile)}
          >
            {mobile ? <Close /> : <Menu />}
          </button>
          <nav aria-label="Workspace navigation">
            {navigation.map(([key, label, Icon]) => (
              <button
                key={key}
                className={view === key ? "active" : ""}
                onClick={() => navigate(key)}
              >
                <Icon size={19} />
                <span>{label}</span>
                {key === "alerts" && !!data?.alerts.length && (
                  <b>{data.alerts.length}</b>
                )}
              </button>
            ))}
          </nav>
          <div className="navbar-account">
            <button
              className="account-profile-button"
              title="Open profile"
              aria-label="Open profile"
              onClick={() => navigate("profile")}
            >
              <span className="avatar">{(session.name || session.email)[0].toUpperCase()}</span>
              <span className="account-copy">
                <strong>{session.name || (session.role === "admin" ? "Administrator" : "Viewer")}</strong>
                <small>{session.email}</small>
              </span>
            </button>
            <button title="Sign out" aria-label="Sign out" onClick={logout}>
              <LogOut size={18} />
            </button>
          </div>
        </div>
      </header>
      <main className="workspace-main">
        <div className="content">
          <div className="page-heading">
            <div>
              <p className="eyebrow">BIHAR / SOCIAL INTELLIGENCE</p>
              <h1>
                {view === "overview"
                  ? "Sentiment overview"
                  : view === "feed"
                    ? "Conversation feed"
                    : view === "alerts"
                      ? "Alert centre"
                      : view === "profile"
                        ? session.role === "admin" ? "Administrator profile" : "User profile"
                        : "Workspace settings"}
              </h1>
              <p>
                {view === "overview"
                  ? "The conversation at a glance. Every signal in context."
                  : view === "feed"
                    ? "Explore the posts behind the numbers."
                    : view === "alerts"
                      ? "Threshold breaches, evidence and notification status."
                      : view === "profile"
                        ? "Update your account details and password."
                        : "Manage the terms and sources you monitor."}
              </p>
            </div>
            {(view === "overview" || view === "feed") && <div className="heading-actions">
              <div
                className={`classification-progress ${!data || data.pending ? "active" : "complete"}`}
                aria-label={!data ? "Classification status is loading" : data.pending ? `${num(data.pending)} posts remaining for classification` : "Classification is up to date"}
              >
                <div className="classification-progress-copy">
                  <span>Classification</span>
                  <strong>{!data ? "Loading" : data.pending ? `${num(data.pending)} remaining` : "Up to date"}</strong>
                </div>
                <div className="classification-progress-track" aria-hidden="true">
                  <i />
                </div>
              </div>
              <button
                onClick={refresh}
                  disabled={syncing}
                aria-label="Refresh monitoring data"
              >
                  <RefreshCw size={16} className={syncing ? "spin" : ""} />
                  {syncing ? "Refreshing…" : "Refresh"}
              </button>
              <button className="btn btn-primary primary" onClick={() => setReport(true)}>
                <Download size={16} />
                Export report
              </button>
            </div>}
          </div>
          {(view === "overview" || view === "feed") && data?.schedule?.enabled && (
            <SyncScheduleProgress schedule={data.schedule} />
          )}
          {error && (
            <div className="error" role="alert">
              {error}{" "}
              <button onClick={() => setRevision((x) => x + 1)}>Retry</button>
            </div>
          )}
          {!data && !error ? (
            <div className="empty">Loading the watchtower…</div>
          ) : (
            data && (
              <>
                {view === "profile" ? (
                  <ProfilePanel session={session} onSaved={onSessionChange} />
                ) : view === "settings" ? (
                  <SettingsPanel
                    admin={session.role === "admin"}
                    onSaved={() => setRevision((x) => x + 1)}
                  />
                ) : view === "alerts" ? (
                  <div className="panel alert-list">
                    <div className="panel-title">
                      <h2>Open alerts</h2>
                      <span>{data.alerts.length} reporting days</span>
                    </div>
                    {data.alerts.length ? (
                      data.alerts.map((a) => (
                        <button
                          className="alert-row"
                          key={a.date}
                          onClick={() => setDetail(a)}
                        >
                          <TriangleAlert />
                          <div>
                            <strong>
                              {dateLabel(a.date)} · Negative volume threshold
                              exceeded
                            </strong>
                            <span>
                              {num(a.negative_count)} negative posts / threshold{" "}
                              {num(a.threshold)}
                            </span>
                          </div>
                          <ChevronRight />
                        </button>
                      ))
                    ) : (
                      <div className="empty">
                        <ShieldCheck />
                        No open threshold alerts.
                      </div>
                    )}
                  </div>
                ) : (
                  <>
                    {active && (
                      <div className="alert-banner">
                        <div className="alert-symbol">
                          <TriangleAlert size={22} />
                        </div>
                        <div>
                          <strong>
                            Negative conversation crossed your daily threshold
                          </strong>
                          <p>
                            <b>{num(active.negative_count)} negative posts</b>{" "}
                            today ·{" "}
                            {num(active.negative_count - active.threshold)}{" "}
                            above the {num(active.threshold)}-post threshold
                          </p>
                        </div>
                        <button onClick={() => setDetail(active)}>
                          Review alert
                          <ArrowUpRight size={16} />
                        </button>
                      </div>
                    )}
                    <div className="platform-tabs">
                      <div>
                        {[["", "All platforms"], ...Object.entries(names)].map(
                          ([key, name]) => (
                            <button
                              key={key}
                              className={platform === key ? "selected" : ""}
                              onClick={() => setPlatform(key)}
                            >
                              {name}
                              {key && (
                                <span
                                  className={
                                    "tiny-dot " +
                                    (data.sources.find(
                                      (s) => s.platform === key,
                                    )?.status === "disconnected"
                                      ? "off"
                                      : "")
                                  }
                                />
                              )}
                            </button>
                          ),
                        )}
                      </div>
                      <span>
                        <CalendarDays size={15} />
                        {dateLabel(data.date)}, {data.date.slice(0, 4)}
                      </span>
                    </div>
                    {disconnected ? (
                      <div className="panel empty">
                        <Radio size={32} />
                        <h2>{names[platform]} is not connected</h2>
                        <p>
                          Connect an owned/managed account or a compliant
                          social-listening provider in Settings.
                        </p>
                        <p>This platform is excluded from all totals.</p>
                        <button onClick={() => setView("settings")}>
                          Manage connection
                          <ChevronRight size={16} />
                        </button>
                      </div>
                    ) : (
                      <>
                        {view === "overview" && (
                          <>
                            <div className="stats">
                              <Stat
                                label="TOTAL CLASSIFIED"
                                value={
                                  data.today ? num(data.today.total_count) : "—"
                                }
                                note={
                                  data.today
                                    ? "Across connected sources"
                                    : "No classified data available"
                                }
                                icon={<Activity size={18} />}
                              />
                              <Stat
                                label="POSITIVE"
                                value={
                                  data.today
                                    ? num(data.today.positive_count)
                                    : "—"
                                }
                                note={
                                  data.today
                                    ? `${((data.today.positive_count / data.today.total_count) * 100).toFixed(1)}% of classified conversation`
                                    : "Awaiting data"
                                }
                                tone="positive"
                              />
                              <Stat
                                label="NEGATIVE"
                                value={
                                  data.today
                                    ? num(data.today.negative_count)
                                    : "—"
                                }
                                note={
                                  data.today
                                    ? `${((data.today.negative_count / data.today.total_count) * 100).toFixed(1)}% of classified conversation`
                                    : "Awaiting data"
                                }
                                tone="negative"
                              />
                              <Stat
                                label="MIXED"
                                value={
                                  data.today
                                    ? num(data.today.mixed_count || 0)
                                    : "â€”"
                                }
                                note={
                                  data.today
                                    ? `${(((data.today.mixed_count || 0) / data.today.total_count) * 100).toFixed(1)}% with both positive and negative stance`
                                    : "Awaiting data"
                                }
                                tone="mixed"
                              />
                              <Stat
                                label="NEGATIVITY INDEX"
                                value={
                                  data.today
                                    ? data.today.negativity_index.toFixed(1) +
                                      "%"
                                    : "—"
                                }
                                note="Negative ÷ classified posts"
                                icon={<Eye size={18} />}
                              />
                            </div>
                            <div className="chart-grid">
                              <section className="panel trend-panel">
                                <div className="panel-title">
                                  <div>
                                    <h2>Conversation pulse</h2>
                                    <p>Daily sentiment · last 30 days</p>
                                  </div>
                                  <div className="legend">
                                    {Object.entries(colors).map(([l, c]) => (
                                      <span key={l}>
                                        <i style={{ background: c }} />
                                        {l}
                                      </span>
                                    ))}
                                  </div>
                                </div>
                                <div className="trend-chart">
                                  <ResponsiveContainer
                                    width="100%"
                                    height="100%"
                                  >
                                    <AreaChart
                                      data={data.trend}
                                      margin={{
                                        left: -23,
                                        right: 12,
                                        top: 12,
                                        bottom: 0,
                                      }}
                                    >
                                      <defs>
                                        {Object.entries(colors).map(
                                          ([l, c]) => (
                                            <linearGradient
                                              key={l}
                                              id={l}
                                              x1="0"
                                              y1="0"
                                              x2="0"
                                              y2="1"
                                            >
                                              <stop
                                                offset="0%"
                                                stopColor={c}
                                                stopOpacity={0.19}
                                              />
                                              <stop
                                                offset="100%"
                                                stopColor={c}
                                                stopOpacity={0.01}
                                              />
                                            </linearGradient>
                                          ),
                                        )}
                                      </defs>
                                      <CartesianGrid
                                        vertical={false}
                                        stroke="#e9edf1"
                                        strokeDasharray="3 3"
                                      />
                                      <XAxis
                                        dataKey="date"
                                        tickFormatter={dateLabel}
                                        minTickGap={32}
                                        tickLine={false}
                                        axisLine={false}
                                        tick={{ fontSize: 12, fill: "#718096" }}
                                      />
                                      <YAxis
                                        tickLine={false}
                                        axisLine={false}
                                        tick={{ fontSize: 12, fill: "#718096" }}
                                      />
                                      <Tooltip
                                        labelFormatter={(d) =>
                                          dateLabel(String(d))
                                        }
                                        contentStyle={{
                                          borderRadius: 6,
                                          borderColor: "#dfe5eb",
                                        }}
                                      />
                                      {Object.entries(colors).map(([l, c]) => (
                                        <Area
                                          key={l}
                                          type="monotone"
                                          dataKey={l + "_count"}
                                          name={l}
                                          stroke={c}
                                          strokeWidth={2.3}
                                          fill={`url(#${l})`}
                                          isAnimationActive={false}
                                        />
                                      ))}
                                    </AreaChart>
                                  </ResponsiveContainer>
                                </div>
                              </section>
                              <section className="panel mix-panel">
                                <div className="panel-title">
                                  <h2>Today’s sentiment</h2>
                                  <span>Share of voice</span>
                                </div>
                                {data.today ? (
                                  <>
                                    <div className="donut">
                                      <ResponsiveContainer
                                        width="100%"
                                        height={190}
                                      >
                                        <PieChart>
                                          <Pie
                                            data={Object.keys(colors).map(
                                              (l) => ({
                                                name: l,
                                                value:
                                                  data.today![
                                                    (l +
                                                      "_count") as keyof Rollup
                                                  ],
                                              }),
                                            )}
                                            dataKey="value"
                                            innerRadius={64}
                                            outerRadius={82}
                                            paddingAngle={3}
                                            stroke="none"
                                            isAnimationActive={false}
                                          >
                                            {Object.values(colors).map((c) => (
                                              <Cell key={c} fill={c} />
                                            ))}
                                          </Pie>
                                          <Tooltip />
                                        </PieChart>
                                      </ResponsiveContainer>
                                      <div className="donut-label">
                                        <strong>
                                          {num(data.today.total_count)}
                                        </strong>
                                        <span>classified posts</span>
                                      </div>
                                    </div>
                                    <div className="mix-legend">
                                      {Object.entries(colors).map(([l, c]) => (
                                        <div key={l}>
                                          <span>
                                            <i style={{ background: c }} />
                                            {l}
                                          </span>
                                          <b>
                                            {num(
                                              Number(
                                                data.today![
                                                  (l + "_count") as keyof Rollup
                                                ],
                                              ),
                                            )}
                                          </b>
                                        </div>
                                      ))}
                                    </div>
                                  </>
                                ) : (
                                  <div className="empty">
                                    No classified data available
                                  </div>
                                )}
                              </section>
                            </div>
                            <div className="secondary-grid">
                              <section className="panel">
                                <div className="panel-title">
                                  <h2>Source coverage</h2>
                                  <span>
                                    {
                                      data.sources.filter(
                                        (s) => s.status !== "disconnected",
                                      ).length
                                    }{" "}
                                    of {data.sources.length} connected
                                  </span>
                                </div>
                                <div className="source-grid">
                                  {data.sources.map((s) => (
                                    <button
                                      key={s.platform}
                                      className="source-item"
                                      onClick={() => setPlatform(s.platform)}
                                    >
                                      <span
                                        className={
                                          "platform-mark " + s.platform
                                        }
                                      >
                                        <PlatformIcon platform={s.platform} />
                                      </span>
                                      <strong>{names[s.platform]}</strong>
                                      <span
                                        className={
                                          s.status === "disconnected"
                                            ? "muted"
                                            : s.status === "unavailable"
                                              ? "source-error"
                                              : "source-state"
                                        }
                                      >
                                        {sourceStateLabel(s)}
                                      </span>
                                      <small>
                                        {s.status === "disconnected"
                                          ? "Excluded from totals"
                                          : s.error || "Includes classified mentions"}
                                      </small>
                                    </button>
                                  ))}
                                </div>
                              </section>
                              <section className="panel heat-panel">
                                <div className="panel-title">
                                  <h2>Negativity calendar</h2>
                                  <span>30 days</span>
                                </div>
                                <div className="heatmap">
                                  {Array.from({ length: 30 }, (_, i) => {
                                    const dt = new Date(
                                      data.date + "T12:00:00",
                                    );
                                    dt.setDate(dt.getDate() - 29 + i);
                                    const day = dt.toISOString().slice(0, 10);
                                    const row = data.trend.find(
                                      (r) => r.date === day,
                                    );
                                    return (
                                      <button
                                        key={day}
                                        title={`${dateLabel(day)}: ${row ? row.negativity_index + "% negative" : "No data"}`}
                                        style={{
                                          background: row
                                            ? `rgba(192,63,62,${0.1 + row.negativity_index / 100})`
                                            : "#eef1f4",
                                        }}
                                        onClick={() => {
                                          setView("feed");
                                          window.dispatchEvent(
                                            new CustomEvent("filter-day", {
                                              detail: day,
                                            }),
                                          );
                                        }}
                                      >
                                        {dt.getDate()}
                                      </button>
                                    );
                                  })}
                                </div>
                                <div className="heat-key">
                                  <span>Less negative</span>
                                  <i />
                                  <i />
                                  <i />
                                  <i />
                                  <span>More negative</span>
                                </div>
                              </section>
                            </div>
                          </>
                        )}
                        <Feed
                          platform={platform}
                          revision={revision}
                          defaultDay={data.date}
                          compact={view === "overview"}
                          onExpand={() => setView("feed")}
                        />
                      </>
                    )}
                  </>
                )}
              </>
            )
          )}
          <footer>
            <span>
              <Eye size={14} /> JanNetra ·{" "}
              Public conversation monitoring
            </span>
            <span>
              Sentiment is a model estimate, not a measure of voting intent.
            </span>
          </footer>
        </div>
      </main>
      {report && <ReportModal close={() => setReport(false)} />}{" "}
      {detail && (
        <Modal
          title={"Threshold alert · " + dateLabel(detail.date)}
          close={() => setDetail(null)}
        >
          <div className="alert-summary">
            <TriangleAlert />
            <strong>{num(detail.negative_count)} negative posts</strong>
            <span>Threshold: {num(detail.threshold)}</span>
          </div>
          <p>
            {Object.entries(detail.platform_breakdown)
              .map(([p, n]) => names[p] + ": " + num(n))
              .join(" · ")}
          </p>
          <h3>Five most-engaged negative posts</h3>
          {detail.top_negative_posts.map((p) => (
            <div className="evidence" key={p._id}>
              <span>
                {names[p.platform]} · {num(p.engagement_score)} interactions
              </span>
              <p>{p.content}</p>
              {p.url && (
                <a href={p.url} target="_blank" rel="noreferrer">
                  View source ↗
                </a>
              )}
            </div>
          ))}
          <p className="muted">
            {`Delivered: ${detail.notified_channels.join(", ") || "None"}`}
          </p>
          {detail.notification_errors && (
            <p className="error">
              Some notifications failed and are scheduled for retry.
            </p>
          )}
          {session.role === "admin" && (
            <button
              className="btn btn-primary primary"
              onClick={async () => {
                try {
                  await json("/alerts/" + detail.date + "/resolve", {
                    method: "POST",
                  });
                  setDetail(null);
                  setRevision((x) => x + 1);
                } catch (e) {
                  setError((e as Error).message);
                }
              }}
            >
              <Check size={16} />
              Resolve alert
            </button>
          )}
        </Modal>
      )}
    </div>
    </GlobalLoadingContext.Provider>
  );
}

function Stat({
  label,
  value,
  note,
  tone = "",
  icon,
}: {
  label: string;
  value: string;
  note: string;
  tone?: string;
  icon?: React.ReactNode;
}) {
  return (
    <section className={"stat " + tone}>
      <div>
        <span>{label}</span>
        {icon || <i />}
      </div>
      <strong>{value}</strong>
      <p>{note}</p>
    </section>
  );
}

function Feed({
  platform,
  revision,
  defaultDay,
  compact,
  onExpand,
}: {
  platform: string;
  revision: number;
  defaultDay: string;
  compact: boolean;
  onExpand: () => void;
}) {
  const beginLoading = React.useContext(GlobalLoadingContext);
  const [q, setQ] = useState(""),
    [term, setTerm] = useState(""),
    [sentiment, setSentiment] = useState(""),
    [sort, setSort] = useState("recency"),
    [day, setDay] = useState(defaultDay),
    [page, setPage] = useState(1),
    [posts, setPosts] = useState<{ items: Post[]; total: number } | null>(null),
    [loading, setLoading] = useState(true),
    [error, setError] = useState("");
  const [expandedPosts, setExpandedPosts] = useState<Set<string>>(
    () => new Set(),
  );
  useEffect(() => {
    const t = setTimeout(() => setTerm(q), 300);
    return () => clearTimeout(t);
  }, [q]);
  useEffect(() => {
    const fn = (e: Event) => setDay((e as CustomEvent).detail);
    window.addEventListener("filter-day", fn);
    return () => window.removeEventListener("filter-day", fn);
  }, []);
  useEffect(() => setPage(1), [platform, term, sentiment, sort, day]);
  useEffect(
    () => setExpandedPosts(new Set()),
    [platform, term, sentiment, sort, page, day],
  );
  useEffect(() => {
    let active = true;
    const finishLoading = beginLoading("Loading conversations…");
    setError("");
    setLoading(true);
    const p = new URLSearchParams({ q: term, sort, page: String(page) });
    if (platform) p.set("platform", platform);
    if (sentiment) p.set("sentiment", sentiment);
    if (day) p.set("day", day);
    json("/posts?" + p)
      .then((p) => active && setPosts(p))
      .catch((e) => active && setError(e.message))
      .finally(() => {
        if (active) setLoading(false);
        finishLoading();
      });
    return () => {
      active = false;
      finishLoading();
    };
  }, [platform, term, sentiment, sort, page, revision, day, beginLoading]);
  return (
    <section className="panel feed">
      <div className="panel-title">
        <div>
          <h2>{compact ? "Conversation watch" : "Recent posts"}</h2>
          <p>
            {loading
              ? "Loading conversations…"
              : posts
                ? num(posts.total) + " matching posts"
                : "Posts unavailable"}
          </p>
        </div>
        {compact && (
          <button className="text-button" onClick={onExpand}>
            View all posts
            <ArrowUpRight size={15} />
          </button>
        )}
      </div>
      <div className="feed-controls">
        <label className="search">
          <Search size={17} />
          <input className="form-control"
            aria-label="Search posts"
            placeholder="Search conversations…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </label>
        <select className="form-select"
          aria-label="Filter sentiment"
          value={sentiment}
          onChange={(e) => setSentiment(e.target.value)}
        >
          <option value="">All sentiments</option>
          <option value="positive">Positive</option>
          <option value="negative">Negative</option>
          <option value="neutral">Neutral</option>
          <option value="mixed">Mixed</option>
        </select>
        <select className="form-select"
          aria-label="Sort posts"
          value={sort}
          onChange={(e) => setSort(e.target.value)}
        >
          <option value="recency">Most recent</option>
          <option value="engagement">Most engaged</option>
        </select>
        {!compact && (
          <input className="form-control"
            type="date"
            aria-label="Filter date"
            value={day}
            onChange={(e) => setDay(e.target.value)}
          />
        )}
        {day && <button onClick={() => setDay("")}>Clear date</button>}
      </div>
      {error ? (
        <p className="error">{error}</p>
      ) : posts?.items.length === 0 ? (
        <div className="empty">No conversations match these filters.</div>
      ) : (
        <div className="post-table">
          <div className="table-heading">
            <span>CONVERSATION</span>
            <span>SENTIMENT</span>
            <span>INTERACTIONS</span>
            <span>POSTED · IST</span>
          </div>
          {posts?.items.slice(0, compact ? 4 : 20).map((p) => {
            const redditExpandable =
              p.platform === "reddit" && p.content.length > 240;
            const redditExpanded = expandedPosts.has(p._id);
            return (
            <article className="post-row" key={p._id}>
              <div>
                <div className="post-author">
                  <span className={"mini-platform " + p.platform}>
                    <PlatformIcon platform={p.platform} />
                  </span>
                  <b>{p.author}</b>
                  <span>
                    {names[p.platform]}
                  </span>
                </div>
                <p
                  className={
                    redditExpandable && !redditExpanded
                      ? "reddit-post-content compact"
                      : p.platform === "reddit"
                        ? "reddit-post-content"
                        : undefined
                  }
                >
                  {p.content}
                </p>
                {(redditExpandable ||
                  (p.url && /^https:\/\//.test(p.url))) && (
                  <div className="post-actions">
                    {p.url && /^https:\/\//.test(p.url) && (
                      <a href={p.url} target="_blank" rel="noreferrer">
                        Open original
                        <ArrowUpRight size={12} />
                      </a>
                    )}
                    {redditExpandable && (
                      <button
                        type="button"
                        className="reddit-more"
                        aria-expanded={redditExpanded}
                        onClick={() =>
                          setExpandedPosts((current) => {
                            const next = new Set(current);
                            if (next.has(p._id)) next.delete(p._id);
                            else next.add(p._id);
                            return next;
                          })
                        }
                      >
                        {redditExpanded ? "Show less" : "More"}
                      </button>
                    )}
                  </div>
                )}
              </div>
              <div>
                {p.sentiment ? (
                  <>
                    <span className={"sentiment " + p.sentiment.label}>
                      {p.sentiment.label}
                    </span>
                    <small title={p.sentiment.reason}>
                      {Math.round(p.sentiment.confidence * 100)}% confidence
                      {p.sentiment.review_required ? " · Review" : ""}
                    </small>
                  </>
                ) : (
                  <span className="muted">{p.classification_status === "awaiting_gemini" ? "Awaiting Gemini" : "Pending"}</span>
                )}
              </div>
              <div>
                <strong>{num(p.engagement_score)}</strong>
                <small>
                  {num(p.engagement.likes)} likes / {p.platform === "youtube"
                    ? `${num(p.engagement.views)} views`
                    : `${num(p.engagement.comments)} replies`}
                </small>
              </div>
              <div>
                {new Date(p.published_at).toLocaleDateString("en-IN", {
                  day: "numeric",
                  month: "short",
                  timeZone: "Asia/Kolkata",
                })}
                <small>
                  {new Date(p.published_at).toLocaleTimeString("en-IN", {
                    hour: "2-digit",
                    minute: "2-digit",
                    timeZone: "Asia/Kolkata",
                  })}
                </small>
              </div>
            </article>
            );
          })}
        </div>
      )}
      {!compact && posts && (
        <div className="pagination">
          <span>
            Page {page} of {Math.max(1, Math.ceil(posts.total / 20))}
          </span>
          <button disabled={page === 1} onClick={() => setPage((p) => p - 1)}>
            Previous
          </button>
          <button
            disabled={page * 20 >= posts.total}
            onClick={() => setPage((p) => p + 1)}
          >
            Next
          </button>
        </div>
      )}
    </section>
  );
}

function Modal({
  title,
  close,
  children,
}: {
  title: string;
  close: () => void;
  children: React.ReactNode;
}) {
  const ref = React.useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    el?.showModal();
    return () => el?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      onCancel={close}
      onClick={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <div className="jn-modal">
        <div className="modal-heading">
          <h2>{title}</h2>
          <button aria-label="Close dialog" onClick={close}>
            <Close size={20} />
          </button>
        </div>
        {children}
      </div>
    </dialog>
  );
}
function ReportModal({ close }: { close: () => void }) {
  const beginLoading = React.useContext(GlobalLoadingContext);
  const [period, setPeriod] = useState("daily"),
    [format, setFormat] = useState("pdf"),
    [sentiment, setSentiment] = useState("all"),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  return (
    <Modal title="Export sentiment report" close={close}>
      <p>
        Reports include classified totals, negativity index, and post evidence.
        Reporting days use Asia/Kolkata.
      </p>
      <label>
        Period
        <select className="form-select" value={period} onChange={(e) => setPeriod(e.target.value)}>
          <option value="daily">Today</option>
          <option value="weekly">Last 7 days</option>
          <option value="monthly">Last 30 days</option>
        </select>
      </label>
      <label>
        Sentiment
        <select className="form-select" value={sentiment} onChange={(e) => setSentiment(e.target.value)}>
          <option value="all">All sentiments</option>
          <option value="positive">Positive</option>
          <option value="negative">Negative</option>
          <option value="mixed">Mixed</option>
          <option value="neutral">Neutral</option>
        </select>
      </label>
      <label>
        File format
        <select className="form-select" value={format} onChange={(e) => setFormat(e.target.value)}>
          <option value="pdf">PDF report</option>
          <option value="csv">CSV spreadsheet</option>
        </select>
      </label>
      {error && <p className="error">{error}</p>}
      <button
        className="btn btn-primary primary"
        disabled={busy}
        onClick={async () => {
          const finishLoading = beginLoading("Preparing your report…");
          setBusy(true);
          try {
            const response = await api(
              `/export?period=${period}&format=${format}&sentiment=${sentiment}`,
            );
            const url = URL.createObjectURL(await response.blob());
            const a = document.createElement("a");
            a.href = url;
            a.download = `jannetra-${period}-${sentiment}.${format}`;
            a.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
            close();
          } catch (e) {
            setError((e as Error).message);
          } finally {
            setBusy(false);
            finishLoading();
          }
        }}
      >
        <Download size={16} />
        {busy ? "Preparing…" : "Download report"}
      </button>
    </Modal>
  );
}

function ProfilePanel({
  session,
  onSaved,
}: {
  session: Session;
  onSaved: (session: Session) => void;
}) {
  const beginLoading = React.useContext(GlobalLoadingContext);
  const [profile, setProfile] = useState({ name: session.name, email: session.email });
  const [savedProfile, setSavedProfile] = useState({ name: session.name, email: session.email });
  const [editing, setEditing] = useState(false);
  const [confirmation, setConfirmation] = useState<"profile" | "password" | null>(null);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [visiblePasswords, setVisiblePasswords] = useState({ current: false, next: false, confirm: false });
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    const finishLoading = beginLoading("Loading your profile?");
    json("/profile")
      .then((value) => {
        const loaded = { name: value.name, email: value.email };
        setProfile(loaded);
        setSavedProfile(loaded);
      })
      .catch((e) => setError(e.message))
      .finally(finishLoading);
    return finishLoading;
  }, [beginLoading]);

  function clearSecurityForm() {
    setCurrentPassword("");
    setNewPassword("");
    setConfirmPassword("");
    setVisiblePasswords({ current: false, next: false, confirm: false });
  }

  async function confirmSave(mode: "profile" | "password") {
    setError("");
    setMessage("");
    if (mode === "password") {
      if (newPassword !== confirmPassword) {
        setError("New password and confirmation do not match.");
        return;
      }
      if (newPassword.length < 8 || newPassword.length > 14) {
        setError("New password must contain 8 to 14 characters.");
        return;
      }
    }
    const finishLoading = beginLoading(mode === "profile" ? "Updating your profile?" : "Changing your password?");
    setSaving(true);
    try {
      const target = mode === "profile" ? profile : savedProfile;
      const result = await json("/profile", {
        method: "PUT",
        body: JSON.stringify({
          name: target.name,
          email: target.email,
          current_password: currentPassword,
          new_password: mode === "password" ? newPassword : "",
        }),
      });
      if (mode === "profile") {
        const saved = { name: result.name, email: result.email };
        setProfile(saved);
        setSavedProfile(saved);
        onSaved({ role: result.role, ...saved });
        setEditing(false);
        setMessage("Profile updated successfully.");
      } else {
        setMessage("Password changed successfully.");
      }
      setConfirmation(null);
      clearSecurityForm();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
      finishLoading();
    }
  }

  function PasswordInput({
    fieldKey,
    label,
    value,
    setValue,
    autoComplete,
  }: {
    fieldKey: "current" | "next" | "confirm";
    label: string;
    value: string;
    setValue: (value: string) => void;
    autoComplete: string;
  }) {
    return (
      <label>
        {label}
        <span className="profile-password-field">
          <input className="form-control" type={visiblePasswords[fieldKey] ? "text" : "password"}
            required minLength={fieldKey === "current" ? 1 : 8}
            maxLength={fieldKey === "current" ? 72 : 14}
            autoComplete={autoComplete} value={value}
            onChange={(e) => setValue(e.target.value)} />
          <button type="button" aria-label={`${visiblePasswords[fieldKey] ? "Hide" : "Show"} ${label.toLowerCase()}`}
            onClick={() => setVisiblePasswords((state) => ({ ...state, [fieldKey]: !state[fieldKey] }))}>
            {visiblePasswords[fieldKey] ? <EyeSlash size={17} /> : <Eye size={17} />}
          </button>
        </span>
      </label>
    );
  }

  return (
    <section className="panel profile-panel">
      <div className="profile-header">
        <span className="profile-avatar">{(profile.name || profile.email)[0]?.toUpperCase()}</span>
        <div>
          <h2>{savedProfile.name || "Administrator"}</h2>
          <p>{session.role === "admin" ? "Administrator account" : "Viewer account"}</p>
        </div>
        {!editing && !confirmation && (
          <div className="profile-header-actions">
            <button type="button" className="btn btn-outline-primary profile-edit-button" onClick={() => {
              setEditing(true);
              setMessage("");
              setError("");
            }}>Edit profile</button>
            <button type="button" className="btn profile-password-button" onClick={() => {
              clearSecurityForm();
              setConfirmation("password");
              setMessage("");
              setError("");
            }}>Change password</button>
          </div>
        )}
      </div>

      <div className="profile-fields">
        <label>
          Full name
          <input className="form-control" minLength={2} maxLength={80} required
            disabled={!editing || confirmation !== null} value={profile.name}
            onChange={(e) => setProfile({ ...profile, name: e.target.value })} />
        </label>
        <label>
          Email address
          <input className="form-control" type="email" required autoComplete="email"
            disabled={!editing || confirmation !== null} value={profile.email}
            onChange={(e) => setProfile({ ...profile, email: e.target.value })} />
        </label>
      </div>

      {editing && !confirmation && (
        <div className="profile-actions">
          <button type="button" className="btn btn-light" onClick={() => {
            setProfile(savedProfile);
            setEditing(false);
            setError("");
          }}>Cancel</button>
          <button type="button" className="btn btn-primary primary" onClick={() => {
            if (profile.name.trim().length < 2 || !profile.email.includes("@")) {
              setError("Enter a valid name and email address.");
              return;
            }
            clearSecurityForm();
            setConfirmation("profile");
            setError("");
          }}>Save profile</button>
        </div>
      )}

      {confirmation === "profile" && (
        <div className="profile-confirmation-box">
          <div><strong>Confirm profile changes</strong><p>Enter your current password before updating your name or email.</p></div>
          <PasswordInput fieldKey="current" label="Current password" value={currentPassword}
            setValue={setCurrentPassword} autoComplete="current-password" />
          <div className="profile-actions">
            <button type="button" className="btn btn-light" disabled={saving} onClick={() => {
              setConfirmation(null);
              clearSecurityForm();
              setError("");
            }}>Cancel</button>
            <button type="button" className="btn btn-primary primary" disabled={saving || !currentPassword}
              onClick={() => confirmSave("profile")}>{saving ? "Confirming?" : "Confirm"}</button>
          </div>
        </div>
      )}

      {confirmation === "password" && (
        <div className="profile-confirmation-box password-change-box">
          <div><strong>Change password</strong><p>Use 8 to 14 characters for the new password.</p></div>
          <div className="profile-fields profile-passwords">
            <PasswordInput fieldKey="current" label="Current password" value={currentPassword}
              setValue={setCurrentPassword} autoComplete="current-password" />
            <PasswordInput fieldKey="next" label="New password" value={newPassword}
              setValue={setNewPassword} autoComplete="new-password" />
            <PasswordInput fieldKey="confirm" label="Confirm new password" value={confirmPassword}
              setValue={setConfirmPassword} autoComplete="new-password" />
          </div>
          <div className="profile-actions">
            <button type="button" className="btn btn-light" disabled={saving} onClick={() => {
              setConfirmation(null);
              clearSecurityForm();
              setError("");
            }}>Cancel</button>
            <button type="button" className="btn btn-primary primary"
              disabled={saving || !currentPassword || newPassword.length < 8 || newPassword.length > 14 || newPassword !== confirmPassword}
              onClick={() => confirmSave("password")}>{saving ? "Saving?" : "Save password"}</button>
          </div>
        </div>
      )}

      {error && <p className="error" role="alert">{error}</p>}
      {message && <p className="success" role="status"><Check size={16} />{message}</p>}
    </section>
  );
}

function SettingsPanel({
  admin,
  onSaved,
}: {
  admin: boolean;
  onSaved: () => void;
}) {
  const beginLoading = React.useContext(GlobalLoadingContext);
  const [prefs, setPrefs] = useState<any>(null),
    [terms, setTerms] = useState(""),
    [startHour, setStartHour] = useState(6),
    [endHour, setEndHour] = useState(22),
    [editing, setEditing] = useState(false),
    [message, setMessage] = useState(""),
    [error, setError] = useState(""),
    [saving, setSaving] = useState(false);
  useEffect(() => {
    const finishLoading = beginLoading("Loading workspace settings…");
    json("/settings")
      .then((p) => {
        setPrefs(p);
        setStartHour(p.automation_start_hour ?? 6);
        setEndHour(p.automation_end_hour ?? 22);
        setTerms(
          p.keywords
            .filter((k: any) => k.is_active)
            .map((k: any) => k.keyword)
            .join("\n"),
        );
      })
      .catch((e) => setError(e.message))
      .finally(finishLoading);
    return finishLoading;
  }, [beginLoading]);
  if (!prefs)
    return <div className="panel empty">{error || "Loading settings…"}</div>;
  async function save(e: React.FormEvent) {
    e.preventDefault();
    const finishLoading = beginLoading("Saving workspace settings…");
    setSaving(true);
    setError("");
    setMessage("");
    try {
      await json("/settings", {
        method: "PUT",
        body: JSON.stringify({
          keywords: terms.split("\n").filter(Boolean),
          automation_start_hour: startHour,
          automation_end_hour: endHour,
        }),
      });
      setPrefs({
        ...prefs,
        automation_start_hour: startHour,
        automation_end_hour: endHour,
        keywords: terms.split("\n").filter(Boolean).map((keyword) => ({ keyword, is_active: true })),
      });
      setEditing(false);
      setMessage("Settings saved. The automatic schedule has been updated.");
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
      finishLoading();
    }
  }
  return (
    <>
      <form onSubmit={save} className="settings-grid" autoComplete="off">
        <section className="panel settings-section tracked-terms-section">
          <div className="panel-title">
            <div><h2>Monitoring settings</h2><span>Hindi · English · Hinglish</span></div>
            {admin && !editing && <button type="button" className="btn btn-outline-primary"
              onClick={() => { setEditing(true); setMessage(""); setError(""); }}>Edit settings</button>}
          </div>

          <label>
            Keywords and hashtags
            <textarea className="form-control"
              rows={9}
              value={terms}
              onChange={(e) => setTerms(e.target.value)}
              disabled={!admin || !editing}
            />
          </label>
          <div className="schedule-editor">
            <div>
              <strong>Active automation window</strong>
              <p>YouTube and Google News run every 15 minutes. Other social sources run every 4 hours.</p>
            </div>
            <label>
              Start time
              <select className="form-select" value={startHour}
                disabled={!admin || !editing} onChange={(e) => setStartHour(Number(e.target.value))}>
                {Array.from({ length: 23 }, (_, hour) => <option key={hour} value={hour}>{String(hour).padStart(2, "0")}:00</option>)}
              </select>
            </label>
            <label>
              End time
              <select className="form-select" value={endHour}
                disabled={!admin || !editing} onChange={(e) => setEndHour(Number(e.target.value))}>
                {Array.from({ length: 23 }, (_, index) => index + 1).map((hour) => <option key={hour} value={hour}>{String(hour).padStart(2, "0")}:00</option>)}
              </select>
            </label>
            <small>Timezone: {prefs.timezone || "Asia/Kolkata"}</small>
          </div>
        </section>
        <div className="settings-save">
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          {message && (
            <p className="success" role="status">
              <Check size={16} />
              {message}
            </p>
          )}
          {admin && editing ? (<>
            <button type="button" className="btn btn-light" disabled={saving} onClick={() => {
              setTerms(prefs.keywords.filter((k: any) => k.is_active).map((k: any) => k.keyword).join("\n"));
              setStartHour(prefs.automation_start_hour ?? 6);
              setEndHour(prefs.automation_end_hour ?? 22);
              setEditing(false);
              setError("");
            }}>Cancel</button>
            <button className="btn btn-primary primary" disabled={saving || startHour >= endHour}>
              {saving ? "Saving…" : "Save settings"}
            </button>
          </>) : !admin ? (
            <p>Viewer access · Ask an administrator to change settings.</p>
          ) : null}
        </div>
      </form>
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
