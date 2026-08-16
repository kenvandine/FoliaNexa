// Shared helpers for the public player hub (portal/). No build step, no
// framework — plain fetch() against mgmt's unauthenticated
// /api/v1/public/* routes (mgmt/src/folia_mgmt/routers/public_stats.py),
// same hand-written style as mgmt's own dashboard.

function resolveApiBase() {
  const meta = document.querySelector('meta[name="api-base"]');
  if (meta && meta.content) return meta.content.replace(/\/$/, "");
  // Best-effort default matching deploy/vps/Caddyfile's default subdomain
  // scheme (play.<domain> / api.<domain>). If you used a different scheme,
  // set the <meta name="api-base"> tag explicitly instead of relying on
  // this guess.
  const guessedHost = window.location.hostname.replace(/^play\./, "api.");
  return `${window.location.protocol}//${guessedHost}/api/v1/public`;
}

const API_BASE = resolveApiBase();

async function apiGet(path) {
  const resp = await fetch(`${API_BASE}${path}`);
  if (!resp.ok) {
    throw new Error(`${resp.status} ${resp.statusText}`);
  }
  return resp.json();
}

function avatarUrl(uuid) {
  // Self-hosted, rendered by mgmt from the player's real Mojang skin
  // (mgmt/src/folia_mgmt/avatar.py) — previously this pointed at
  // crafatar.com, a free third-party avatar service, but that's an
  // outside dependency with its own uptime this project doesn't control
  // (confirmed down, Cloudflare 521, on 2026-08-16 — every avatar on the
  // portal broke at once). mgmt falls back to a placeholder image itself
  // if the skin can't be resolved, so this URL is never actually broken.
  return `${API_BASE}/players/${encodeURIComponent(uuid)}/avatar?size=64`;
}

function formatPlaytime(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours === 0) return `${minutes}m`;
  return `${hours}h ${minutes}m`;
}

function formatStatLabel(statKey) {
  return statKey.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatRelativeTime(isoString) {
  const then = new Date(isoString + "Z"); // mgmt stores naive-UTC timestamps
  const diffMs = Date.now() - then.getTime();
  const diffMin = Math.round(diffMs / 60000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  return `${Math.round(diffHr / 24)}d ago`;
}

// A player counts as "recently active" if we heard from them (via the
// stats plugin's periodic report) within this window. This is a proxy for
// "online now", not a real presence feed — v1 deliberately has no
// WebSocket/live-push layer (see docs/vps-edge-deployment.md), so treat
// this as approximate and label it as such in the UI rather than
// overclaiming exact online counts.
const RECENTLY_ACTIVE_WINDOW_MINUTES = 5;

function isRecentlyActive(lastSeenIso) {
  const then = new Date(lastSeenIso + "Z");
  return (Date.now() - then.getTime()) / 60000 <= RECENTLY_ACTIVE_WINDOW_MINUTES;
}

function renderHeatmap(container, playtimeDaily) {
  const byDate = new Map(playtimeDaily.map((d) => [d.date, d.seconds]));
  const max = Math.max(1, ...playtimeDaily.map((d) => d.seconds));
  const today = new Date();
  const days = [];
  for (let i = 364; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(d.getDate() - i);
    const key = d.toISOString().slice(0, 10);
    days.push({ date: key, seconds: byDate.get(key) || 0 });
  }
  container.innerHTML = "";
  for (const day of days) {
    const cell = document.createElement("div");
    cell.className = "day";
    const intensity = day.seconds / max;
    if (day.seconds > 0) {
      const alpha = 0.25 + 0.75 * intensity;
      cell.style.background = `rgba(79, 140, 255, ${alpha.toFixed(2)})`;
    }
    cell.title = `${day.date}: ${formatPlaytime(day.seconds)}`;
    container.appendChild(cell);
  }
}

function showError(container, err) {
  container.innerHTML = `<p class="error">Couldn't load data from ${API_BASE}: ${err.message}</p>`;
}
