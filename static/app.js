const state = {
  competitions: [],
  currentCode: null,
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
        <div class="card-title">${m.home} ${m.score} ${m.away}</div>
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
      <div class="card-title">${p.home} vs ${p.away}</div>
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
    el("#results-odds").innerHTML = odds.map((o) => `
      <div class="card">
        <div class="card-title">${o.home} vs ${o.away}</div>
        <div class="card-row"><span class="label">Domicile</span><span class="value">${Math.round(o.probabilities.home * 100)}%</span></div>
        ${o.probabilities.draw !== null ? `<div class="card-row"><span class="label">Nul</span><span class="value">${Math.round(o.probabilities.draw * 100)}%</span></div>` : ""}
        <div class="card-row"><span class="label">Extérieur</span><span class="value">${Math.round(o.probabilities.away * 100)}%</span></div>
      </div>
    `).join("");
  } catch (e) { showError("#results-odds", `Erreur : ${e.message}`); }
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
        <div class="card-title">${m.home} ${m.score} ${m.away}</div>
        <div class="card-date">Minute ${m.minute ?? "?"}</div>
        ${m.alert ? `<div class="signal-line">⚠️ ${m.alert}</div>` : ""}
      </div>
    `).join("");
  } catch (e) { showError("#results-live", `Erreur : ${e.message}`); }
}

// ---------------------- Câblage des boutons ----------------------
function setupButtons() {
  const actions = { check: runCheck, predict: runPredict, elo: runElo, odds: runOdds, value: runValue, live: runLive };
  document.querySelectorAll(".run-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      btn.disabled = true;
      Promise.resolve(actions[btn.dataset.action]()).finally(() => { btn.disabled = false; });
    });
  });
}

// ---------------------- Démarrage ----------------------
(async function init() {
  setupTabs();
  setupButtons();
  loadStatus();
  await loadCompetitions();
})();
