const state = {
  competitions: [],
  currentCode: null,
  notifEnabled: false,
  seenSuspicions: new Set(),
};

const el = (sel) => document.querySelector(sel);

// ---------------------- Service worker (installabilité) ----------------------
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

// ---------------------- Statut des APIs ----------------------
async function loadStatus() {
  try {
    const r = await fetch("/api/status");
    const s = await r.json();
    const dot = el("#statusDot");
    if (s.football_data && s.api_football && s.odds_api) {
      dot.className = "status-dot ok";
      dot.title = "Toutes les APIs sont configurées";
    } else if (s.football_data) {
      dot.className = "status-dot partial";
      dot.title = "Football-data OK — API-Football et/ou Odds API non configurées";
    } else {
      dot.title = "Aucune API configurée";
    }
  } catch (e) { /* silencieux */ }
}

// ---------------------- Notifications (pendant que l'app est ouverte) ----------------------
function setupNotifications() {
  const btn = el("#notifBtn");
  if (!("Notification" in window)) {
    btn.style.display = "none";
    return;
  }
  if (Notification.permission === "granted") {
    state.notifEnabled = true;
    btn.classList.add("enabled");
  }
  btn.addEventListener("click", async () => {
    if (Notification.permission === "granted") {
      state.notifEnabled = !state.notifEnabled;
      btn.classList.toggle("enabled", state.notifEnabled);
      return;
    }
    const perm = await Notification.requestPermission();
    state.notifEnabled = perm === "granted";
    btn.classList.toggle("enabled", state.notifEnabled);
  });
}

function notifyNewSuspicions(code, results) {
  if (!state.notifEnabled) return;
  for (const r of results) {
    if (r.suspicion_score < 50) continue;
    const key = `${code}:${r.home}:${r.away}`;
    if (state.seenSuspicions.has(key)) continue;
    state.seenSuspicions.add(key);
    try {
      new Notification("⚠️ Signal MatchGuard", {
        body: `${r.home} vs ${r.away} — score de suspicion ${r.suspicion_score}/100`,
        icon: "/static/icon-192.png",
      });
    } catch (e) { /* silencieux si le navigateur refuse */ }
  }
}

// ---------------------- Compétitions ----------------------
async function loadCompetitions() {
  const r = await fetch("/api/competitions");
  const data = await r.json();
  state.competitions = Object.entries(data);
  const select = el("#compSelect");
  select.innerHTML = state.competitions
    .map(([code, name]) => `<option value="${code}">${name}</option>`)
    .join("");
  state.currentCode = state.competitions[0][0];
  select.addEventListener("change", () => { state.currentCode = select.value; });
}

// ---------------------- Onglets ----------------------
function setupTabs() {
  const tabs = document.querySelectorAll(".tab");
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
      el(`#panel-${tab.dataset.tab}`).classList.add("active");
    });
  });
}

// ---------------------- Aides d'affichage ----------------------
function showLoading(containerId, label) {
  el(containerId).innerHTML = `<div class="state-msg">🔎 ${label}...</div>`;
}

function showError(containerId, message) {
  el(containerId).innerHTML = `<div class="state-msg error">${message}</div>`;
}

function showEmpty(containerId, message) {
  el(containerId).innerHTML = `<div class="state-msg">${message}</div>`;
}

async function fetchJSON(url) {
  const r = await fetch(url);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || "Erreur inconnue");
  return data;
}

function crestImg(url, alt) {
  return url ? `<img src="${url}" alt="${alt}" class="team-crest" loading="lazy" onerror="this.style.display='none'">` : "";
}

function emblemImg(url) {
  return url ? `<img src="${url}" alt="" class="comp-emblem" loading="lazy" onerror="this.style.display='none'">` : "";
}

function formatLocalDateTime(isoString) {
  const d = new Date(isoString);
  const datePart = d.toLocaleDateString(undefined, { weekday: "short", day: "2-digit", month: "short" });
  const timePart = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return `${datePart} — ${timePart}`;
}

// ---------------------- Matchs à venir (/fixtures) ----------------------
async function runFixtures() {
  const code = state.currentCode;
  showLoading("#results-fixtures", "Récupération des matchs à venir");
  try {
    const data = await fetchJSON(`/api/fixtures/${code}`);
    const fixtures = data.fixtures || [];
    if (fixtures.length === 0) {
      showEmpty("#results-fixtures", "Aucun match programmé dans les 7 prochains jours pour ce championnat.");
      return;
    }
    el("#results-fixtures").innerHTML = fixtures.map((f) => `
      <div class="card">
        <div class="card-title">${emblemImg(f.competition_emblem)}${crestImg(f.home_crest, f.home)} ${f.home} vs ${f.away} ${crestImg(f.away_crest, f.away)}</div>
        <div class="card-date">${formatLocalDateTime(f.utc_date)}${f.matchday ? ` · Journée ${f.matchday}` : ""}</div>
      </div>
    `).join("");
  } catch (e) { showError("#results-fixtures", `Erreur : ${e.message}`); }
}

// ---------------------- Anomalies (/check) ----------------------
async function runCheck() {
  const code = state.currentCode;
  showLoading("#results-check", "Analyse des matchs récents");
  try {
    const data = await fetchJSON(`/api/check/${code}`);
    const matches = data.matches || [];
    if (matches.length === 0) {
      showEmpty("#results-check", "✅ Aucun match hors norme détecté récemment.");
      return;
    }
    el("#results-check").innerHTML = matches.map((m) => `
      <div class="card alert">
        <div class="card-title">${emblemImg(m.competition_emblem)}${crestImg(m.home_crest, m.home)} ${m.home} ${m.score} ${m.away} ${crestImg(m.away_crest, m.away)}</div>
        <div class="card-date">${m.date}</div>
        ${m.signals.map((s) => `<div class="signal-line">[${s.type}] ${s.detail}</div>`).join("")}
      </div>
    `).join("");
  } catch (e) { showError("#results-check", `Erreur : ${e.message}`); }
}

// ---------------------- Pronostics (/predict) ----------------------
function renderPrediction(p) {
  const lines = Object.entries(p.over_under).map(([line, v]) =>
    `<div class="card-row"><span class="label">Total ${line}</span><span class="value">Plus ${v.over}% · Moins ${v.under}%</span></div>`
  ).join("");

  const groups = Object.entries(p.score_groups).map(([label, pct]) =>
    `<div class="card-row"><span class="label">${label}</span><span class="value">${pct}%</span></div>`
  ).join("");

  const scores = p.top_scores.map((s) => `${s.score} (${s.probability}%)`).join(" · ");

  const eloLine = (p.elo_home && p.elo_away)
    ? `<div class="card-row"><span class="label">Élo</span><span class="value">${p.home} ${p.elo_home} · ${p.away} ${p.elo_away}</span></div>`
    : "";

  const formLine = (p.home_form || p.away_form)
    ? `<div class="card-row"><span class="label">Forme (5 derniers)</span><span class="value">${p.home_form || "?"} · ${p.away_form || "?"}</span></div>`
    : "";

  const absencesLines = p.absences && p.absences.length
    ? p.absences.map((a) => `<div class="signal-line">${a.player} (${a.team}) — ${a.reason}</div>`).join("")
    : "";

  return `
    <div class="card">
      <div class="card-title">${emblemImg(p.competition_emblem)}${crestImg(p.home_crest, p.home)} ${p.home} vs ${p.away} ${crestImg(p.away_crest, p.away)}</div>
      <div class="card-date">${p.date}</div>
      ${eloLine}
      ${formLine}
      <div class="card-row"><span class="label">Buts attendus</span><span class="value">${p.lambda_home} · ${p.lambda_away}</span></div>
      <div class="card-row"><span class="label">1X2</span><span class="value">${p.p_home_win}% · ${p.p_draw}% · ${p.p_away_win}%</span></div>
      <div class="card-row"><span class="label">2 équipes marquent</span><span class="value">${p.p_btts_yes}%</span></div>
      ${lines}
      <div class="card-row"><span class="label">Scores probables</span><span class="value">${scores}</span></div>
      ${groups}
      ${absencesLines}
    </div>
  `;
}

async function runPredict() {
  const code = state.currentCode;
  showLoading("#results-predict", "Calcul des pronostics");
  try {
    const data = await fetchJSON(`/api/predict/${code}`);
    const preds = data.predictions || [];
    if (preds.length === 0) {
      showEmpty("#results-predict", "Aucun match à venir (ou pas assez d'historique) dans les 7 prochains jours.");
      return;
    }
    el("#results-predict").innerHTML = preds.map(renderPrediction).join("");
  } catch (e) { showError("#results-predict", `Erreur : ${e.message}`); }
}

// ---------------------- Élo (/elo + /elochart) ----------------------
async function runElo() {
  const home = el("#eloHome").value.trim();
  const away = el("#eloAway").value.trim();
  if (!home) {
    showEmpty("#results-elo", "Renseigne au moins une équipe.");
    return;
  }
  showLoading("#results-elo", "Récupération de l'Élo");
  try {
    const homeData = await fetchJSON(`/api/elo/${encodeURIComponent(home)}`);
    let html = `
      <div class="card">
        <div class="card-title">${homeData.team}</div>
        <div class="card-row"><span class="label">Élo brut</span><span class="value">${homeData.elo_raw}</span></div>
        <div class="card-row"><span class="label">Note sur 99</span><span class="value">${homeData.elo_display}</span></div>
      </div>
    `;
    if (away) {
      try {
        const awayData = await fetchJSON(`/api/elo/${encodeURIComponent(away)}`);
        html += `
          <div class="card">
            <div class="card-title">${awayData.team}</div>
            <div class="card-row"><span class="label">Élo brut</span><span class="value">${awayData.elo_raw}</span></div>
            <div class="card-row"><span class="label">Note sur 99</span><span class="value">${awayData.elo_display}</span></div>
          </div>
        `;
      } catch (e) { /* équipe 2 non trouvée, on continue avec juste l'équipe 1 */ }
    }
    const chartUrl = `/api/elochart?home=${encodeURIComponent(home)}&away=${encodeURIComponent(away || home)}&t=${Date.now()}`;
    html += `<img class="elo-chart-img" src="${chartUrl}" alt="Progression Élo" onerror="this.style.display='none'">`;
    el("#results-elo").innerHTML = html;
  } catch (e) { showError("#results-elo", `Erreur : ${e.message}`); }
}

// ---------------------- Cotes (/odds) ----------------------
async function runOdds() {
  const code = state.currentCode;
  showLoading("#results-odds", "Récupération des cotes");
  try {
    const data = await fetchJSON(`/api/odds/${code}`);
    const odds = data.odds || [];
    if (odds.length === 0) {
      showEmpty("#results-odds", "Aucune cote disponible actuellement pour ce championnat.");
      return;
    }
    el("#results-odds").innerHTML = odds.map((o) => {
      const p = o.probabilities;
      const overround = p.overround_pct !== undefined ? p.overround_pct : null;
      const totalsLines = Object.entries(o.totals || {}).map(([line, t]) => `
        <div class="card-row"><span class="label">Plus ${line} (net / brut)</span><span class="value">${Math.round(t.over * 100)}% / ${Math.round(t.over_raw * 100)}%</span></div>
        <div class="card-row"><span class="label">Moins ${line} (net / brut)</span><span class="value">${Math.round(t.under * 100)}% / ${Math.round(t.under_raw * 100)}%</span></div>
        <div class="card-row"><span class="label">Marge (${line})</span><span class="badge amber">${t.overround_pct}%</span></div>
      `).join("");
      return `
      <div class="card">
        <div class="card-title">${o.home} vs ${o.away}</div>
        <div class="card-row"><span class="label">Domicile (net / brut)</span><span class="value">${Math.round(p.home * 100)}% / ${Math.round((p.home_raw ?? p.home) * 100)}%</span></div>
        ${p.draw !== null ? `<div class="card-row"><span class="label">Nul (net / brut)</span><span class="value">${Math.round(p.draw * 100)}% / ${Math.round((p.draw_raw ?? p.draw) * 100)}%</span></div>` : ""}
        <div class="card-row"><span class="label">Extérieur (net / brut)</span><span class="value">${Math.round(p.away * 100)}% / ${Math.round((p.away_raw ?? p.away) * 100)}%</span></div>
        ${overround !== null ? `<div class="card-row"><span class="label">Marge 1X2</span><span class="badge amber">${overround}%</span></div>` : ""}
        ${totalsLines}
      </div>
    `;
    }).join("");
  } catch (e) { showError("#results-odds", `Erreur : ${e.message}`); }
}

// ---------------------- Score de suspicion combiné (/suspicion) ----------------------
async function runSuspicion() {
  const code = state.currentCode;
  showLoading("#results-suspicion", "Calcul du score de suspicion");
  try {
    const data = await fetchJSON(`/api/suspicion/${code}`);
    const results = data.results || [];
    if (results.length === 0) {
      showEmpty("#results-suspicion", "Aucun signal combiné pour l'instant (value bet ou mouvement de cote).");
      return;
    }
    notifyNewSuspicions(code, results);
    el("#results-suspicion").innerHTML = results.map((r) => {
      const cls = r.suspicion_score >= 50 ? "alert" : "";
      return `
      <div class="card ${cls}">
        <div class="card-title">${r.home} vs ${r.away}</div>
        <div class="card-row"><span class="label">Score de suspicion</span><span class="badge ${r.suspicion_score >= 50 ? "amber" : ""}">${r.suspicion_score}/100</span></div>
        <div class="card-row"><span class="label">Écart value</span><span class="value">${r.value_edge}pt</span></div>
        <div class="card-row"><span class="label">Mouvement cotes</span><span class="value">${r.odds_movement}pt</span></div>
      </div>
    `;
    }).join("");
  } catch (e) { showError("#results-suspicion", `Erreur : ${e.message}`); }
}

// ---------------------- Suivi de fiabilité (/calibration) ----------------------
async function runCalibration() {
  showLoading("#results-calibration", "Chargement du suivi");
  try {
    const data = await fetchJSON("/api/calibration");
    if (!data.total_resolved) {
      showEmpty("#results-calibration", `${data.total_tracked} pronostic(s) en attente de résultat. Reviens après que les matchs soient joués pour voir la fiabilité du modèle.`);
      return;
    }
    let html = `
      <div class="card">
        <div class="card-title">Fiabilité globale (${data.total_resolved} match(s) résolus sur ${data.total_tracked} suivis)</div>
        <div class="card-row"><span class="label">1X2 correct</span><span class="value">${data.hit_rate_1x2}%</span></div>
        <div class="card-row"><span class="label">Plus/Moins 2.5 correct</span><span class="value">${data.hit_rate_over25}%</span></div>
        <div class="card-row"><span class="label">2 équipes marquent correct</span><span class="value">${data.hit_rate_btts}%</span></div>
      </div>
    `;
    html += (data.recent || []).map((e) => `
      <div class="card">
        <div class="card-title">${e.home} vs ${e.away}</div>
        <div class="card-date">${e.date} — score réel ${e.actual.score}</div>
        <div class="card-row"><span class="label">1X2</span><span class="value">${e.pick_1x2} ${e.actual.hit_1x2 ? "✅" : "❌"}</span></div>
        <div class="card-row"><span class="label">Plus/Moins 2.5</span><span class="value">${e.pick_over25} ${e.actual.hit_over25 ? "✅" : "❌"}</span></div>
        <div class="card-row"><span class="label">2 équipes marquent</span><span class="value">${e.pick_btts} ${e.actual.hit_btts ? "✅" : "❌"}</span></div>
      </div>
    `).join("");
    el("#results-calibration").innerHTML = html;
  } catch (e) { showError("#results-calibration", `Erreur : ${e.message}`); }
}

// ---------------------- Value bets (/value) ----------------------
async function runValue() {
  const code = state.currentCode;
  showLoading("#results-value", "Recherche de value bets");
  try {
    const data = await fetchJSON(`/api/value/${code}`);
    const bets = data.value_bets || [];
    if (bets.length === 0) {
      showEmpty("#results-value", "Aucune value détectée (écart < 5 points), ou pas assez de correspondances entre les deux sources.");
      return;
    }
    el("#results-value").innerHTML = bets.map((b) => `
      <div class="card value-pos">
        <div class="card-title">${b.home} vs ${b.away}</div>
        <div class="card-date">${b.date}</div>
        <div class="card-row"><span class="label">${b.side}</span><span class="badge green">+${b.edge}pt</span></div>
        <div class="card-row"><span class="label">Modèle</span><span class="value">${b.model_prob}%</span></div>
        <div class="card-row"><span class="label">Marché</span><span class="value">${b.market_prob}%</span></div>
      </div>
    `).join("");
  } catch (e) { showError("#results-value", `Erreur : ${e.message}`); }
}

// ---------------------- Live (/live) ----------------------
async function runLive() {
  const code = state.currentCode;
  showLoading("#results-live", "Vérification des matchs en direct");
  try {
    const data = await fetchJSON(`/api/live/${code}`);
    const matches = data.matches || [];
    if (matches.length === 0) {
      showEmpty("#results-live", "Aucun match en direct actuellement pour ce championnat.");
      return;
    }
    el("#results-live").innerHTML = matches.map((m) => `
      <div class="card ${m.alert ? "alert" : ""}">
        <div class="card-title">${emblemImg(m.competition_emblem)}${crestImg(m.home_crest, m.home)} ${m.home} ${m.score} ${m.away} ${crestImg(m.away_crest, m.away)}</div>
        <div class="card-date">Minute ${m.minute ?? "?"}</div>
        ${m.alert ? `<div class="signal-line">⚠️ ${m.alert}</div>` : ""}
      </div>
    `).join("");
  } catch (e) { showError("#results-live", `Erreur : ${e.message}`); }
}

// ---------------------- Câblage des boutons ----------------------
function setupButtons() {
  const actions = {
    fixtures: runFixtures, check: runCheck, predict: runPredict, suspicion: runSuspicion, elo: runElo,
    odds: runOdds, value: runValue, live: runLive, calibration: runCalibration,
  };
  document.querySelectorAll(".run-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      btn.disabled = true;
      Promise.resolve(actions[btn.dataset.action]()).finally(() => { btn.disabled = false; });
    });
  });
}

// ---------------------- Démarrage ----------------------
function setupTelegramWebApp() {
  // Best effort : si l'app est ouverte dans Telegram (Mini App), adapte
  // l'affichage (plein écran, thème). Sans effet dans un navigateur normal.
  const tg = window.Telegram && window.Telegram.WebApp;
  if (!tg) return;
  try {
    tg.ready();
    tg.expand();
    if (tg.setHeaderColor) tg.setHeaderColor("#0a1420");
    if (tg.setBackgroundColor) tg.setBackgroundColor("#0a1420");
  } catch (e) { /* silencieux si une méthode n'est pas supportée */ }
}

(async function init() {
  setupTelegramWebApp();
  setupTabs();
  setupButtons();
  setupNotifications();
  loadStatus();
  await loadCompetitions();
})();
