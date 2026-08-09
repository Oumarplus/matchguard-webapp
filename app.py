"""
MatchGuard — application web de statistiques football
========================================================

Reprend toutes les fonctionnalités du bot Telegram (Stats_foot bot) sous
forme d'API web + interface installable sur téléphone (PWA).

⚠️ Ceci fournit des estimations statistiques, PAS des preuves de matchs
truqués ni des garanties de gain. À utiliser comme signal d'alerte /
outil d'aide à la décision, jamais comme certitude.
"""

import os
import re
import csv
import json
import math
import time
import logging
import threading
import unicodedata
import statistics
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, render_template, send_from_directory, request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder=".")

# --- Configuration ---
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY")
FOOTBALL_API_BASE = "https://api.football-data.org/v4"
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY")
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

COMPETITIONS = {
    "PL": "Premier League",
    "PD": "La Liga",
    "FL1": "Ligue 1",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "CL": "Ligue des Champions",
    "BSA": "Brasileirão (Brésil)",
    "DED": "Eredivisie (Pays-Bas)",
    "PPL": "Primeira Liga (Portugal)",
}

ODDS_SPORT_KEYS = {
    "PL": "soccer_epl",
    "PD": "soccer_spain_la_liga",
    "FL1": "soccer_france_ligue_one",
    "SA": "soccer_italy_serie_a",
    "BL1": "soccer_germany_bundesliga",
    "CL": "soccer_uefa_champs_league",
    "BSA": "soccer_brazil_campeonato",
    "DED": "soccer_netherlands_eredivisie",
}

Z_SCORE_THRESHOLD = 2.0
HISTORY_WINDOW = 10
SECOND_HALF_SHARE_THRESHOLD = 0.75
SECOND_HALF_MIN_GOALS = 3
H2H_MIN_MATCHES = 3
PREDICT_DAYS_AHEAD = 7
MAX_PREDICT_MATCHES = 3  # limite le nb de matchs analysés par requête (évite un timeout serveur)
PREDICT_HISTORY_WINDOW = 8
GOAL_LINES = [1.5, 2.5, 3.5]
MAX_GOALS_SIMULATED = 8
VALUE_EDGE_THRESHOLD = 0.05
VALUE_GOAL_LINE = 2.5
LIVE_BURST_GOALS_THRESHOLD = 2
LIVE_BURST_MINUTES_WINDOW = 15

STATE_DIR = "state"
os.makedirs(STATE_DIR, exist_ok=True)
LIVE_STATE_FILE = os.path.join(STATE_DIR, "live_state.json")
ODDS_STATE_FILE = os.path.join(STATE_DIR, "odds_state.json")
PREDICTIONS_LOG_FILE = os.path.join(STATE_DIR, "predictions_log.json")

FD_MIN_INTERVAL_SECONDS = 6.5
FD_MAX_RETRIES = 2
_fd_last_call_time = 0.0
_team_id_cache = {}


# ---------------------- football-data.org (avec limiteur de débit) ----------------------

def fd_get(endpoint: str, params: dict = None) -> dict:
    global _fd_last_call_time
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}

    for attempt in range(FD_MAX_RETRIES + 1):
        elapsed = time.time() - _fd_last_call_time
        if elapsed < FD_MIN_INTERVAL_SECONDS:
            time.sleep(FD_MIN_INTERVAL_SECONDS - elapsed)

        resp = requests.get(f"{FOOTBALL_API_BASE}{endpoint}", headers=headers, params=params, timeout=15)
        _fd_last_call_time = time.time()

        if resp.status_code == 429 and attempt < FD_MAX_RETRIES:
            wait_seconds = int(resp.headers.get("Retry-After", 60))
            logger.warning(f"football-data.org 429, nouvelle tentative dans {wait_seconds}s...")
            time.sleep(wait_seconds)
            continue

        resp.raise_for_status()
        return resp.json()


def _attach_competition_emblem(data: dict) -> list:
    """L'emblème du championnat est renvoyé une fois au niveau global de la
    réponse (pas par match) — on le recopie sur chaque match pour l'avoir
    facilement disponible dans le frontend."""
    emblem = data.get("competition", {}).get("emblem")
    matches = data.get("matches", [])
    for m in matches:
        m["_comp_emblem"] = emblem
    return matches


def get_finished_matches(competition_code: str, days_back: int = 14) -> list:
    date_from = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = datetime.utcnow().strftime("%Y-%m-%d")
    data = fd_get(
        f"/competitions/{competition_code}/matches",
        params={"status": "FINISHED", "dateFrom": date_from, "dateTo": date_to},
    )
    return _attach_competition_emblem(data)


def get_upcoming_matches(competition_code: str, days_ahead: int = PREDICT_DAYS_AHEAD) -> list:
    date_from = datetime.utcnow().strftime("%Y-%m-%d")
    date_to = (datetime.utcnow() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    data = fd_get(
        f"/competitions/{competition_code}/matches",
        params={"status": "SCHEDULED", "dateFrom": date_from, "dateTo": date_to},
    )
    return _attach_competition_emblem(data)


def get_live_matches(competition_code: str) -> list:
    data = fd_get(f"/competitions/{competition_code}/matches", params={"status": "LIVE"})
    return _attach_competition_emblem(data)


def get_team_recent_totals(team_id: int, before_date: str) -> list:
    data = fd_get(f"/teams/{team_id}/matches", params={"status": "FINISHED", "limit": HISTORY_WINDOW + 5})
    totals = []
    for m in data.get("matches", []):
        if m["utcDate"] >= before_date:
            continue
        h, a = m["score"]["fullTime"]["home"], m["score"]["fullTime"]["away"]
        if h is None or a is None:
            continue
        totals.append(h + a)
        if len(totals) >= HISTORY_WINDOW:
            break
    return totals


def get_team_scored_conceded(team_id: int, is_home: bool, limit: int = PREDICT_HISTORY_WINDOW) -> tuple:
    data = fd_get(f"/teams/{team_id}/matches", params={"status": "FINISHED", "limit": limit + 10})
    matches = data.get("matches", [])
    matches.sort(key=lambda m: m["utcDate"], reverse=True)

    scored, conceded, form = [], [], []
    for m in matches:
        h, a = m["score"]["fullTime"]["home"], m["score"]["fullTime"]["away"]
        if h is None or a is None:
            continue
        team_is_home = m["homeTeam"]["id"] == team_id
        if team_is_home != is_home:
            continue
        team_goals = h if team_is_home else a
        opp_goals = a if team_is_home else h
        scored.append(team_goals)
        conceded.append(opp_goals)
        form.append("V" if team_goals > opp_goals else "N" if team_goals == opp_goals else "D")
        if len(scored) >= limit:
            break
    return scored, conceded, form


# ---------------------- Détection d'anomalies (matchs joués) ----------------------

def analyze_goal_count(match: dict) -> dict | None:
    home, away = match["homeTeam"], match["awayTeam"]
    score = match["score"]["fullTime"]
    if score["home"] is None or score["away"] is None:
        return None
    total_goals = score["home"] + score["away"]
    match_date = match["utcDate"]
    home_hist = get_team_recent_totals(home["id"], match_date)
    away_hist = get_team_recent_totals(away["id"], match_date)
    combined = home_hist + away_hist
    if len(combined) < 6:
        return None
    mean = statistics.mean(combined)
    stdev = statistics.pstdev(combined) or 0.5
    z = (total_goals - mean) / stdev
    if abs(z) < Z_SCORE_THRESHOLD:
        return None
    return {"type": "buts", "detail": f"Total buts: {total_goals} (attendu: {round(mean, 2)}, z-score: {round(z, 2)})"}


def analyze_half_split(match: dict) -> dict | None:
    full = match["score"]["fullTime"]
    half = match["score"].get("halfTime", {})
    if full["home"] is None or full["away"] is None or half.get("home") is None or half.get("away") is None:
        return None
    total_goals = full["home"] + full["away"]
    first_half = half["home"] + half["away"]
    second_half = total_goals - first_half
    if total_goals < SECOND_HALF_MIN_GOALS:
        return None
    share = second_half / total_goals
    if share < SECOND_HALF_SHARE_THRESHOLD:
        return None
    return {"type": "mi-temps", "detail": f"{second_half}/{total_goals} buts en 2e mi-temps ({round(share*100)}%)"}


def analyze_head_to_head(match: dict) -> dict | None:
    full = match["score"]["fullTime"]
    if full["home"] is None or full["away"] is None:
        return None
    total_goals = full["home"] + full["away"]
    try:
        data = fd_get(f"/matches/{match['id']}/head2head", params={"limit": 10})
    except requests.RequestException:
        return None
    past = []
    for m in data.get("matches", []):
        if m["id"] == match["id"]:
            continue
        s = m["score"]["fullTime"]
        if s["home"] is None or s["away"] is None:
            continue
        past.append(s["home"] + s["away"])
    if len(past) < H2H_MIN_MATCHES:
        return None
    mean = statistics.mean(past)
    stdev = statistics.pstdev(past) or 0.5
    z = (total_goals - mean) / stdev
    if abs(z) < Z_SCORE_THRESHOLD:
        return None
    return {"type": "face-à-face", "detail": f"Historique: {round(mean,2)} buts/moy. sur {len(past)} matchs (z-score: {round(z,2)})"}


def analyze_match(match: dict) -> dict | None:
    home, away = match["homeTeam"], match["awayTeam"]
    score = match["score"]["fullTime"]
    if score["home"] is None or score["away"] is None:
        return None
    signals = []
    for fn in (analyze_goal_count, analyze_half_split, analyze_head_to_head):
        try:
            result = fn(match)
        except requests.RequestException:
            result = None
        if result:
            signals.append(result)
    if not signals:
        return None
    return {
        "home": home["name"], "away": away["name"],
        "home_crest": home.get("crest"), "away_crest": away.get("crest"),
        "competition_emblem": match.get("_comp_emblem"),
        "score": f"{score['home']}-{score['away']}",
        "date": match["utcDate"][:10],
        "signals": signals,
    }


def find_suspicious_matches(competition_code: str) -> list:
    matches = get_finished_matches(competition_code)
    matches = matches[-MAX_PREDICT_MATCHES:]  # borne le temps total (limiteur de débit + Render timeout)
    results = [analyze_match(m) for m in matches]
    results = [r for r in results if r]
    results.sort(key=lambda x: len(x["signals"]), reverse=True)
    return results


# ---------------------- Elo maison (calculé depuis les résultats réels) ----------------------
#
# Remplace l'ancienne dépendance à ClubElo (service tiers instable, bloque le
# scraping automatisé) par un calcul Elo classique entretenu localement :
# - Amorçage ponctuel depuis football-data.co.uk (résultats historiques en
#   libre accès) pour 7 des 9 compétitions suivies (championnats domestiques
#   européens classiques ; ne couvre pas le Brasileirão ni la Ligue des
#   Champions, qui démarrent à ELO_DEFAULT_RATING et convergent avec le temps).
# - Mise à jour continue à partir des résultats terminés de football-data.org.

ELO_DISPLAY_FLOOR = 1350
ELO_DISPLAY_CEILING = 1700

ELO_STATE_FILE = os.path.join(STATE_DIR, "homemade_elo_state.json")
ELO_DEFAULT_RATING = 1500.0
ELO_K_FACTOR = 20
ELO_HOME_ADVANTAGE = 60
ELO_HISTORY_MAX_DAYS = 730
ELO_UPDATE_MIN_INTERVAL_SECONDS = 6 * 3600

FD_COUK_LEAGUE_CODES = {
    "PL": "E0", "PD": "SP1", "FL1": "F1", "SA": "I1",
    "BL1": "D1", "DED": "N1", "PPL": "P1",
}
FD_COUK_SEED_SEASONS = ["2324", "2425", "2526"]

ELO_NAME_ABBREVIATIONS = {
    "man city": "manchester city", "man utd": "manchester united", "man united": "manchester united",
    "spurs": "tottenham hotspur", "nott'm forest": "nottingham forest", "wolves": "wolverhampton wanderers",
    "psg": "paris saint germain", "paris sg": "paris saint germain", "bayern munich": "bayern munchen",
    "inter": "internazionale", "ac milan": "milan", "atletico madrid": "atletico de madrid",
    "sp gijon": "sporting gijon", "betis": "real betis", "sociedad": "real sociedad",
    "ath bilbao": "athletic bilbao", "ath madrid": "atletico madrid", "vallecano": "rayo vallecano",
    "leicester": "leicester city", "leeds": "leeds united", "west brom": "west bromwich albion",
    "newcastle": "newcastle united", "west ham": "west ham united", "brighton": "brighton and hove albion",
}

_elo_state_cache = None
_elo_last_update_by_comp = {}


def scale_elo_for_display(raw_elo: float) -> int:
    pct = (raw_elo - ELO_DISPLAY_FLOOR) / (ELO_DISPLAY_CEILING - ELO_DISPLAY_FLOOR)
    pct = min(max(pct, 0.0), 1.0)
    return round(1 + pct * 98)


def _elo_normalize_name(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    n = n.lower().strip()
    n = ELO_NAME_ABBREVIATIONS.get(n, n)
    for suffix in [" fc", " cf", " afc", " sc", " calcio", " club"]:
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    for prefix in ["fc ", "cf ", "afc ", "sc ", "1. ", "1.fc ", "ss ", "ssc ", "us ", "as "]:
        if n.startswith(prefix):
            n = n[len(prefix):]
    n = re.sub(r"\s+", " ", n).strip()
    n = ELO_NAME_ABBREVIATIONS.get(n, n)
    return n


def load_elo_state() -> dict:
    global _elo_state_cache
    if _elo_state_cache is not None:
        return _elo_state_cache
    if not os.path.exists(ELO_STATE_FILE):
        _elo_state_cache = {"ratings": {}, "history": {}, "processed_matches": [], "seen_competitions": [], "seeded": False}
        return _elo_state_cache
    with open(ELO_STATE_FILE, "r") as f:
        _elo_state_cache = json.load(f)
    _elo_state_cache.setdefault("ratings", {})
    _elo_state_cache.setdefault("history", {})
    _elo_state_cache.setdefault("processed_matches", [])
    _elo_state_cache.setdefault("seen_competitions", [])
    _elo_state_cache.setdefault("seeded", False)
    return _elo_state_cache


def save_elo_state(state: dict):
    global _elo_state_cache
    _elo_state_cache = state
    with open(ELO_STATE_FILE, "w") as f:
        json.dump(state, f)


def _elo_apply_match(state: dict, home_name: str, away_name: str, home_goals: int, away_goals: int, match_date: str):
    home_key = _elo_normalize_name(home_name)
    away_key = _elo_normalize_name(away_name)
    ratings = state["ratings"]
    history = state["history"]

    elo_home = ratings.get(home_key, ELO_DEFAULT_RATING)
    elo_away = ratings.get(away_key, ELO_DEFAULT_RATING)

    expected_home = 1 / (1 + 10 ** ((elo_away - (elo_home + ELO_HOME_ADVANTAGE)) / 400))
    if home_goals > away_goals:
        actual_home = 1.0
    elif home_goals == away_goals:
        actual_home = 0.5
    else:
        actual_home = 0.0

    goal_diff = abs(home_goals - away_goals)
    g = 1.0 if goal_diff <= 1 else (1.5 if goal_diff == 2 else (11 + goal_diff) / 8)

    delta = ELO_K_FACTOR * g * (actual_home - expected_home)
    ratings[home_key] = elo_home + delta
    ratings[away_key] = elo_away - delta

    history.setdefault(home_key, []).append([match_date, ratings[home_key]])
    history.setdefault(away_key, []).append([match_date, ratings[away_key]])

    cutoff = (datetime.utcnow() - timedelta(days=ELO_HISTORY_MAX_DAYS)).strftime("%Y-%m-%d")
    history[home_key] = [h for h in history[home_key] if h[0] >= cutoff]
    history[away_key] = [h for h in history[away_key] if h[0] >= cutoff]


def _fd_couk_fetch_matches(fd_code: str, season: str) -> list:
    url = f"https://www.football-data.co.uk/mmz4281/{season}/{fd_code}.csv"
    resp = requests.get(url, timeout=20, headers={"User-Agent": "MatchGuard/1.0"})
    resp.raise_for_status()
    reader = csv.DictReader(resp.text.splitlines())
    matches = []
    for row in reader:
        try:
            date_str = row["Date"]
            fmt = "%d/%m/%Y" if len(date_str.split("/")[-1]) == 4 else "%d/%m/%y"
            date = datetime.strptime(date_str, fmt)
            home, away = row["HomeTeam"], row["AwayTeam"]
            hg, ag = int(row["FTHG"]), int(row["FTAG"])
        except (KeyError, ValueError):
            continue
        matches.append((date, home, away, hg, ag))
    return matches


def seed_homemade_elo():
    state = load_elo_state()
    if state.get("seeded"):
        return
    all_matches = []
    for comp_code, fd_code in FD_COUK_LEAGUE_CODES.items():
        for season in FD_COUK_SEED_SEASONS:
            try:
                all_matches.extend(_fd_couk_fetch_matches(fd_code, season))
            except requests.RequestException as exc:
                logger.warning(f"Amorçage Elo: échec récupération {fd_code}/{season}: {exc}")
    all_matches.sort(key=lambda m: m[0])
    for date, home, away, hg, ag in all_matches:
        _elo_apply_match(state, home, away, hg, ag, date.strftime("%Y-%m-%d"))
    state["seeded"] = True
    save_elo_state(state)
    logger.info(f"Elo maison amorcé avec {len(all_matches)} match(s) historique(s) (football-data.co.uk)")


def update_homemade_elo(competition_code: str):
    state = load_elo_state()
    is_first_run = competition_code not in state.get("seen_competitions", [])
    lookback = 300 if is_first_run else 5

    try:
        matches = get_finished_matches(competition_code, days_back=lookback)
    except requests.RequestException as exc:
        logger.warning(f"Elo maison: erreur récupération résultats {competition_code}: {exc}")
        return

    matches.sort(key=lambda m: m["utcDate"])
    processed = set(state["processed_matches"])
    new_count = 0
    for m in matches:
        mid = m.get("id")
        if mid in processed:
            continue
        score = m.get("score", {}).get("fullTime", {})
        home_goals, away_goals = score.get("home"), score.get("away")
        if home_goals is None or away_goals is None:
            continue
        _elo_apply_match(state, m["homeTeam"]["name"], m["awayTeam"]["name"], home_goals, away_goals, m["utcDate"][:10])
        processed.add(mid)
        new_count += 1

    state["processed_matches"] = list(processed)
    if competition_code not in state["seen_competitions"]:
        state["seen_competitions"].append(competition_code)
    save_elo_state(state)
    _elo_last_update_by_comp[competition_code] = time.time()
    if new_count:
        logger.info(f"Elo maison: {new_count} match(s) traité(s) pour {competition_code}")


def ensure_elo_updated(competition_code: str):
    """Met à jour l'Elo d'une compétition si ça fait plus de
    ELO_UPDATE_MIN_INTERVAL_SECONDS depuis la dernière fois. Comme Flask n'a
    pas de planificateur intégré, cet appel se fait de façon paresseuse (lors
    d'une requête), mais reste rapide car borné à un court lookback après le
    premier passage."""
    last = _elo_last_update_by_comp.get(competition_code, 0)
    if time.time() - last > ELO_UPDATE_MIN_INTERVAL_SECONDS:
        seed_homemade_elo()
        update_homemade_elo(competition_code)


def get_team_elo(team_name: str) -> float | None:
    state = load_elo_state()
    return state["ratings"].get(_elo_normalize_name(team_name))


def get_team_elo_history(team_name: str, days: int = 365) -> list:
    state = load_elo_state()
    raw = state["history"].get(_elo_normalize_name(team_name), [])
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    result = [(datetime.strptime(d, "%Y-%m-%d"), e) for d, e in raw if d >= cutoff]
    result.sort(key=lambda x: x[0])
    return result


def _elo_background_updater():
    """Tourne dans un thread daemon séparé, en boucle indéfiniment : amorce
    (une fois) puis met à jour le système Elo maison pour toutes les
    compétitions suivies. Flask n'a pas de planificateur intégré comme le
    job_queue du bot Telegram, donc ce thread en tient lieu — démarré une
    fois au chargement du module, avant même le premier appel HTTP."""
    while True:
        try:
            seed_homemade_elo()
            for code in COMPETITIONS:
                update_homemade_elo(code)
                time.sleep(1)  # petite marge de courtoisie entre compétitions
        except Exception as exc:
            logger.warning(f"Erreur mise à jour Elo en arrière-plan: {exc}")
        time.sleep(6 * 3600)


threading.Thread(target=_elo_background_updater, daemon=True).start()


# ---------------------- Blessures / suspensions (API-Football) ----------------------

def af_get(endpoint: str, params: dict = None):
    if not API_FOOTBALL_KEY:
        return None
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    try:
        resp = requests.get(f"{API_FOOTBALL_BASE}{endpoint}", headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def af_find_team_id(team_name: str) -> int | None:
    if team_name in _team_id_cache:
        return _team_id_cache[team_name]
    data = af_get("/teams", params={"search": team_name})
    if not data or not data.get("response"):
        return None
    team_id = data["response"][0]["team"]["id"]
    _team_id_cache[team_name] = team_id
    return team_id


def get_absences(home_name: str, away_name: str, match_date: str) -> list | None:
    if not API_FOOTBALL_KEY:
        return None
    home_id = af_find_team_id(home_name)
    if not home_id:
        return None
    data = af_get("/fixtures", params={"team": home_id, "date": match_date})
    if not data or not data.get("response"):
        return None
    fixture_id = data["response"][0]["fixture"]["id"]
    inj_data = af_get("/injuries", params={"fixture": fixture_id})
    if not inj_data:
        return None
    absences = []
    for entry in inj_data.get("response", []):
        player, team = entry.get("player", {}), entry.get("team", {})
        absences.append({
            "player": player.get("name", "?"), "team": team.get("name", "?"),
            "reason": player.get("reason") or player.get("type") or "raison inconnue",
        })
    return absences


# ---------------------- Prédictions (modèle de Poisson) ----------------------

def poisson_pmf(k: int, lam: float) -> float:
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def build_score_matrix(lambda_home: float, lambda_away: float) -> list:
    return [[poisson_pmf(i, lambda_home) * poisson_pmf(j, lambda_away) for j in range(MAX_GOALS_SIMULATED + 1)]
            for i in range(MAX_GOALS_SIMULATED + 1)]


def predict_match(match: dict) -> dict | None:
    home, away = match["homeTeam"], match["awayTeam"]
    home_scored, home_conceded, home_form = get_team_scored_conceded(home["id"], is_home=True)
    away_scored, away_conceded, away_form = get_team_scored_conceded(away["id"], is_home=False)
    if len(home_scored) < 3 or len(away_scored) < 3:
        return None

    lambda_home = max((statistics.mean(home_scored) + statistics.mean(away_conceded)) / 2, 0.1)
    lambda_away = max((statistics.mean(away_scored) + statistics.mean(home_conceded)) / 2, 0.1)
    matrix = build_score_matrix(lambda_home, lambda_away)
    n = len(matrix)

    p_home_win = sum(matrix[i][j] for i in range(n) for j in range(n) if i > j)
    p_draw = sum(matrix[i][i] for i in range(n))
    p_away_win = sum(matrix[i][j] for i in range(n) for j in range(n) if i < j)
    p_btts = (1 - poisson_pmf(0, lambda_home)) * (1 - poisson_pmf(0, lambda_away))

    over_under = {}
    for line in GOAL_LINES:
        threshold = math.floor(line)
        p_under = sum(matrix[i][j] for i in range(n) for j in range(n) if i + j <= threshold)
        over_under[str(line)] = {"over": round((1 - p_under) * 100, 1), "under": round(p_under * 100, 1)}

    scorelines = sorted(
        ([f"{i}-{j}", matrix[i][j]] for i in range(n) for j in range(n)),
        key=lambda x: x[1], reverse=True
    )
    top_scores = [{"score": s, "probability": round(p * 100, 1)} for s, p in scorelines[:3]]

    def group_prob(scores):
        total = 0.0
        for s in scores:
            i, j = map(int, s.split("-"))
            if i < n and j < n:
                total += matrix[i][j]
        return round(total * 100, 1)

    score_groups = {
        "Victoire dom. nette": group_prob(["1-0", "2-0", "3-0"]),
        "Match nul": group_prob(["1-1", "2-2", "3-3"]),
        "Victoire dom. courte": group_prob(["2-1", "3-1", "4-1"]),
        "Victoire ext. nette": group_prob(["0-1", "0-2", "0-3"]),
        "Victoire ext. courte": group_prob(["1-2", "1-3", "1-4"]),
    }

    elo_home, elo_away = get_team_elo(home["name"]), get_team_elo(away["name"])
    absences = get_absences(home["name"], away["name"], match["utcDate"][:10])

    return {
        "match_id": match["id"],
        "home_crest": home.get("crest"), "away_crest": away.get("crest"),
        "competition_emblem": match.get("_comp_emblem"),
        "home": home["name"], "away": away["name"],
        "date": match["utcDate"][:16].replace("T", " "),
        "lambda_home": round(lambda_home, 2), "lambda_away": round(lambda_away, 2),
        "p_home_win": round(p_home_win * 100, 1), "p_draw": round(p_draw * 100, 1), "p_away_win": round(p_away_win * 100, 1),
        "p_btts_yes": round(p_btts * 100, 1),
        "over_under": over_under, "top_scores": top_scores, "score_groups": score_groups,
        "home_form": "-".join(home_form[:5]) if home_form else None,
        "away_form": "-".join(away_form[:5]) if away_form else None,
        "elo_home": scale_elo_for_display(elo_home) if elo_home else None,
        "elo_away": scale_elo_for_display(elo_away) if elo_away else None,
        "absences": absences,
    }


def get_fixtures(competition_code: str, days_ahead: int = PREDICT_DAYS_AHEAD) -> list:
    """Liste simple des matchs à venir avec heure — sans calcul de pronostic,
    utile même quand /predict ne peut rien afficher (pas assez d'historique)."""
    matches = get_upcoming_matches(competition_code, days_ahead)
    out = []
    for m in matches:
        out.append({
            "home": m["homeTeam"]["name"], "away": m["awayTeam"]["name"],
            "home_crest": m["homeTeam"].get("crest"), "away_crest": m["awayTeam"].get("crest"),
            "competition_emblem": m.get("_comp_emblem"),
            "utc_date": m["utcDate"],
            "matchday": m.get("matchday"),
        })
    out.sort(key=lambda x: x["utc_date"])
    return out


def get_predictions(competition_code: str) -> list:
    matches = get_upcoming_matches(competition_code)
    matches.sort(key=lambda m: m["utcDate"])
    matches = matches[:MAX_PREDICT_MATCHES]  # borne le temps total (limiteur de débit + Render timeout)
    results = []
    for m in matches:
        try:
            p = predict_match(m)
        except requests.RequestException:
            p = None
        if p:
            results.append(p)
    return results


# ---------------------- Suivi de la fiabilité du modèle ----------------------

def log_predictions(predictions: list):
    """Enregistre chaque pronostic (choix du modèle) pour comparaison future
    avec le résultat réel, une fois le match joué."""
    log = load_json_state(PREDICTIONS_LOG_FILE)
    for p in predictions:
        mid = str(p["match_id"])
        if mid in log:
            continue  # déjà enregistré, on ne l'écrase pas
        outcomes = {"Dom": p["p_home_win"], "Nul": p["p_draw"], "Ext": p["p_away_win"]}
        pick_1x2 = max(outcomes, key=outcomes.get)
        over25 = p["over_under"].get("2.5", {})
        pick_ou25 = "Plus" if over25.get("over", 0) >= over25.get("under", 0) else "Moins"
        pick_btts = "Oui" if p["p_btts_yes"] >= 50 else "Non"
        log[mid] = {
            "home": p["home"], "away": p["away"], "date": p["date"],
            "pick_1x2": pick_1x2, "pick_over25": pick_ou25, "pick_btts": pick_btts,
            "actual": None,
        }
    save_json_state(PREDICTIONS_LOG_FILE, log)


def get_match_result(match_id) -> dict | None:
    try:
        data = fd_get(f"/matches/{match_id}")
    except requests.RequestException:
        return None
    return data.get("match") or data


def update_calibration_log() -> dict:
    """Pour chaque pronostic en attente dont la date est passée, va chercher
    le résultat réel et calcule si le modèle avait vu juste."""
    log = load_json_state(PREDICTIONS_LOG_FILE)
    now = datetime.utcnow()
    changed = False

    for mid, entry in log.items():
        if entry.get("actual") is not None:
            continue
        try:
            match_date = datetime.strptime(entry["date"][:16], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if match_date > now - timedelta(hours=2):
            continue  # le match n'est probablement pas encore terminé

        result = get_match_result(mid)
        if not result:
            continue
        score = result.get("score", {}).get("fullTime", {})
        h, a = score.get("home"), score.get("away")
        if h is None or a is None:
            continue  # pas encore joué ou reporté

        actual_1x2 = "Dom" if h > a else ("Ext" if a > h else "Nul")
        actual_total = h + a
        actual_ou25 = "Plus" if actual_total > 2.5 else "Moins"
        actual_btts = "Oui" if (h > 0 and a > 0) else "Non"

        entry["actual"] = {
            "score": f"{h}-{a}",
            "1x2": actual_1x2, "over25": actual_ou25, "btts": actual_btts,
            "hit_1x2": entry["pick_1x2"] == actual_1x2,
            "hit_over25": entry["pick_over25"] == actual_ou25,
            "hit_btts": entry["pick_btts"] == actual_btts,
        }
        changed = True

    if changed:
        save_json_state(PREDICTIONS_LOG_FILE, log)
    return log


def get_calibration_stats() -> dict:
    log = update_calibration_log()
    resolved = [e for e in log.values() if e.get("actual") is not None]
    total = len(resolved)
    if total == 0:
        return {"total_tracked": len(log), "total_resolved": 0}

    def hit_rate(key):
        return round(100 * sum(1 for e in resolved if e["actual"][key]) / total, 1)

    return {
        "total_tracked": len(log),
        "total_resolved": total,
        "hit_rate_1x2": hit_rate("hit_1x2"),
        "hit_rate_over25": hit_rate("hit_over25"),
        "hit_rate_btts": hit_rate("hit_btts"),
        "recent": sorted(resolved, key=lambda e: e["date"], reverse=True)[:10],
    }


# ---------------------- Score de suspicion combiné ----------------------

SUSPICION_VALUE_WEIGHT = 2.0
SUSPICION_MOVEMENT_WEIGHT = 1.5


def get_combined_suspicion(competition_code: str) -> list:
    """Combine, pour les matchs à venir, l'écart de value bet et le
    mouvement de cotes en un seul score de suspicion (0-100). Ne concerne
    que les matchs à venir — les anomalies post-match (onglet Anomalies)
    restent un signal séparé, car elles s'appliquent à des matchs déjà joués."""
    sport_key = ODDS_SPORT_KEYS.get(competition_code)
    if not sport_key:
        return []

    value_bets = {(v["home"], v["away"]): v for v in find_value_bets(competition_code)}
    odds_now = get_odds_snapshot(competition_code)
    state = load_json_state(ODDS_STATE_FILE)

    results = []
    for o in odds_now:
        key = (o["home"], o["away"])
        value_edge = value_bets.get(key, {}).get("edge", 0)

        event_state_key = f"{o['home']}__{o['away']}"
        previous = state.get(event_state_key)
        movement = 0.0
        if previous:
            prev_probs = previous.get("probabilities", {})
            cur_probs = o["probabilities"]
            deltas = [abs(cur_probs.get(k, 0) - prev_probs.get(k, 0)) for k in ("home", "draw", "away")
                     if cur_probs.get(k) is not None and prev_probs.get(k) is not None]
            movement = round(max(deltas) * 100, 1) if deltas else 0.0
        state[event_state_key] = {"probabilities": o["probabilities"], "checked_at": datetime.utcnow().isoformat()}

        score = min(100, round(value_edge * SUSPICION_VALUE_WEIGHT + movement * SUSPICION_MOVEMENT_WEIGHT, 1))
        if score <= 0:
            continue
        results.append({
            "home": o["home"], "away": o["away"],
            "value_edge": value_edge, "odds_movement": movement,
            "suspicion_score": score,
        })

    save_json_state(ODDS_STATE_FILE, state)
    results.sort(key=lambda x: x["suspicion_score"], reverse=True)
    return results


# ---------------------- Cotes & value bets (The Odds API) ----------------------

def odds_get(endpoint: str, params: dict = None):
    if not ODDS_API_KEY:
        return None
    params = dict(params or {})
    params["apiKey"] = ODDS_API_KEY
    try:
        resp = requests.get(f"{ODDS_API_BASE}{endpoint}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def get_current_odds(sport_key: str) -> list:
    data = odds_get(f"/sports/{sport_key}/odds", params={"regions": "eu,uk", "markets": "h2h,totals", "oddsFormat": "decimal"})
    return data or []


def implied_probabilities_h2h(event: dict) -> dict | None:
    home_probs, draw_probs, away_probs = [], [], []
    home_raw_probs, draw_raw_probs, away_raw_probs = [], [], []
    overrounds = []
    home_name, away_name = event.get("home_team"), event.get("away_team")
    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market["key"] != "h2h":
                continue
            outcomes = {o["name"]: o["price"] for o in market["outcomes"]}
            if home_name not in outcomes or away_name not in outcomes:
                continue
            raw = {k: 1 / v for k, v in outcomes.items() if v}
            total = sum(raw.values())
            if total == 0:
                continue
            overrounds.append(total - 1)  # marge du bookmaker sur ce marché
            home_raw_probs.append(raw.get(home_name, 0))
            away_raw_probs.append(raw.get(away_name, 0))
            draw_raw_probs.append(raw.get("Draw", 0))
            home_probs.append(raw.get(home_name, 0) / total)
            away_probs.append(raw.get(away_name, 0) / total)
            draw_probs.append(raw.get("Draw", 0) / total)
    if not home_probs:
        return None
    return {
        "home": round(statistics.mean(home_probs), 3),
        "draw": round(statistics.mean(draw_probs), 3) if draw_probs else None,
        "away": round(statistics.mean(away_probs), 3),
        "home_raw": round(statistics.mean(home_raw_probs), 3),
        "draw_raw": round(statistics.mean(draw_raw_probs), 3) if draw_raw_probs else None,
        "away_raw": round(statistics.mean(away_raw_probs), 3),
        "overround_pct": round(statistics.mean(overrounds) * 100, 1),
    }


def implied_probabilities_totals(event: dict, target_point: float = 2.5) -> dict | None:
    over_probs, under_probs = [], []
    over_raw_probs, under_raw_probs = [], []
    overrounds = []
    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market["key"] != "totals":
                continue
            outcomes = {o["name"]: (o["price"], o.get("point")) for o in market["outcomes"]}
            over, under = outcomes.get("Over"), outcomes.get("Under")
            if not over or not under or over[1] != target_point or under[1] != target_point:
                continue
            raw_over, raw_under = 1 / over[0], 1 / under[0]
            total = raw_over + raw_under
            if total == 0:
                continue
            overrounds.append(total - 1)
            over_raw_probs.append(raw_over)
            under_raw_probs.append(raw_under)
            over_probs.append(raw_over / total)
            under_probs.append(raw_under / total)
    if not over_probs:
        return None
    return {
        "over": round(statistics.mean(over_probs), 3),
        "under": round(statistics.mean(under_probs), 3),
        "over_raw": round(statistics.mean(over_raw_probs), 3),
        "under_raw": round(statistics.mean(under_raw_probs), 3),
        "overround_pct": round(statistics.mean(overrounds) * 100, 1),
    }


def normalize_team_name(name: str) -> str:
    name = name.lower()
    for tok in (" fc", "fc ", " cf", "cf ", ".", "-", "'"):
        name = name.replace(tok, "")
    return name.replace(" ", "")


def teams_match(name_a: str, name_b: str) -> bool:
    a, b = normalize_team_name(name_a), normalize_team_name(name_b)
    return a == b or a in b or b in a


def find_matching_odds_event(home_name: str, away_name: str, events: list) -> dict | None:
    for event in events:
        if teams_match(home_name, event.get("home_team", "")) and teams_match(away_name, event.get("away_team", "")):
            return event
    return None


def get_odds_snapshot(competition_code: str) -> list:
    sport_key = ODDS_SPORT_KEYS.get(competition_code)
    if not sport_key:
        return []
    events = get_current_odds(sport_key)
    results = []
    for event in events:
        probs = implied_probabilities_h2h(event)
        if not probs:
            continue
        totals = {}
        for line in (2.5, 3.5):
            t = implied_probabilities_totals(event, line)
            if t:
                totals[str(line)] = t
        results.append({
            "home": event.get("home_team"), "away": event.get("away_team"),
            "probabilities": probs, "totals": totals,
        })
    return results


def find_value_bets(competition_code: str) -> list:
    sport_key = ODDS_SPORT_KEYS.get(competition_code)
    if not sport_key:
        return []
    matches = get_upcoming_matches(competition_code)
    matches.sort(key=lambda m: m["utcDate"])
    matches = matches[:MAX_PREDICT_MATCHES]
    events = get_current_odds(sport_key)
    if not events:
        return []

    results = []
    for match in matches:
        home_name, away_name = match["homeTeam"]["name"], match["awayTeam"]["name"]
        try:
            prediction = predict_match(match)
        except requests.RequestException:
            prediction = None
        if not prediction:
            continue
        model_probs = prediction["over_under"].get(str(VALUE_GOAL_LINE))
        if not model_probs:
            continue
        event = find_matching_odds_event(home_name, away_name, events)
        if not event:
            continue
        market_probs = implied_probabilities_totals(event, VALUE_GOAL_LINE)
        if not market_probs:
            continue

        model_over, model_under = model_probs["over"] / 100, model_probs["under"] / 100
        edge_over = model_over - market_probs["over"]
        edge_under = model_under - market_probs["under"]
        best_side, best_edge = None, 0.0
        if edge_over > best_edge:
            best_side, best_edge = "Plus de 2.5", edge_over
        if edge_under > best_edge:
            best_side, best_edge = "Moins de 2.5", edge_under

        if best_side and best_edge >= VALUE_EDGE_THRESHOLD:
            results.append({
                "home": home_name, "away": away_name, "date": match["utcDate"][:10],
                "side": best_side,
                "model_prob": round((model_over if "Plus" in best_side else model_under) * 100, 1),
                "market_prob": round((market_probs["over"] if "Plus" in best_side else market_probs["under"]) * 100, 1),
                "edge": round(best_edge * 100, 1),
            })
    results.sort(key=lambda x: x["edge"], reverse=True)
    return results


# ---------------------- Surveillance live ----------------------

def load_json_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def save_json_state(path: str, state: dict):
    with open(path, "w") as f:
        json.dump(state, f)


def get_live_snapshot(competition_code: str) -> list:
    matches = get_live_matches(competition_code)
    state = load_json_state(LIVE_STATE_FILE)
    results = []
    for m in matches:
        match_id = str(m["id"])
        score = m["score"]["fullTime"]
        home_goals, away_goals = score.get("home"), score.get("away")
        if home_goals is None or away_goals is None:
            continue
        current_total = home_goals + away_goals
        current_minute = m.get("minute")
        previous = state.get(match_id)
        state[match_id] = {"total_goals": current_total, "minute": current_minute}

        alert = None
        if previous:
            goals_since = current_total - previous["total_goals"]
            minutes_elapsed = None
            if current_minute is not None and previous.get("minute") is not None:
                minutes_elapsed = current_minute - previous["minute"]
            if goals_since >= LIVE_BURST_GOALS_THRESHOLD and (minutes_elapsed is None or minutes_elapsed <= LIVE_BURST_MINUTES_WINDOW):
                alert = f"{goals_since} but(s) rapprochés"

        results.append({
            "home": m["homeTeam"]["name"], "away": m["awayTeam"]["name"],
            "home_crest": m["homeTeam"].get("crest"), "away_crest": m["awayTeam"].get("crest"),
            "competition_emblem": m.get("_comp_emblem"),
            "score": f"{home_goals}-{away_goals}", "minute": current_minute, "alert": alert,
        })
    save_json_state(LIVE_STATE_FILE, state)
    return results


# ---------------------- Routes Flask ----------------------

@app.route("/")
def index():
    return render_template("index.html", competitions=COMPETITIONS)


@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json")


@app.route("/sw.js")
def service_worker():
    return send_from_directory("static", "sw.js")


@app.route("/api/competitions")
def api_competitions():
    return jsonify(COMPETITIONS)


@app.route("/api/check/<code>")
def api_check(code):
    code = code.upper()
    if code not in COMPETITIONS:
        return jsonify({"error": "Code inconnu"}), 404
    try:
        return jsonify({"competition": COMPETITIONS[code], "matches": find_suspicious_matches(code)})
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/fixtures/<code>")
def api_fixtures(code):
    code = code.upper()
    if code not in COMPETITIONS:
        return jsonify({"error": "Code inconnu"}), 404
    try:
        return jsonify({"competition": COMPETITIONS[code], "fixtures": get_fixtures(code)})
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/predict/<code>")
def api_predict(code):
    code = code.upper()
    if code not in COMPETITIONS:
        return jsonify({"error": "Code inconnu"}), 404
    try:
        predictions = get_predictions(code)
        log_predictions(predictions)
        return jsonify({"competition": COMPETITIONS[code], "predictions": predictions})
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/calibration")
def api_calibration():
    return jsonify(get_calibration_stats())


@app.route("/api/suspicion/<code>")
def api_suspicion(code):
    code = code.upper()
    if not ODDS_API_KEY:
        return jsonify({"error": "ODDS_API_KEY non configurée"}), 503
    if code not in COMPETITIONS:
        return jsonify({"error": "Code inconnu"}), 404
    if code not in ODDS_SPORT_KEYS:
        return jsonify({"error": "Cotes non disponibles pour cette compétition"}), 404
    try:
        return jsonify({"competition": COMPETITIONS[code], "results": get_combined_suspicion(code)})
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/elo/<team_name>")
def api_elo(team_name):
    elo = get_team_elo(team_name)
    if elo is None:
        return jsonify({"error": "Équipe non trouvée"}), 404
    return jsonify({"team": team_name, "elo_raw": round(elo), "elo_display": scale_elo_for_display(elo)})


@app.route("/api/elochart")
def api_elochart():
    home = request.args.get("home", "")
    away = request.args.get("away", home)
    if not home:
        return jsonify({"error": "Paramètre 'home' requis"}), 400

    home_hist = get_team_elo_history(home)
    away_hist = get_team_elo_history(away) if away != home else []
    if not home_hist and not away_hist:
        return jsonify({"error": "Aucune donnée Élo trouvée"}), 404

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 5))
    if home_hist:
        d, e = zip(*home_hist)
        ax.plot(d, e, label=home, color="#0ff0fc", linewidth=2.5)
    if away_hist:
        d, e = zip(*away_hist)
        ax.plot(d, e, label=away, color="#ffd166", linewidth=2.5)
    ax.set_facecolor("#0d1b2a")
    fig.patch.set_facecolor("#0d1b2a")
    ax.set_title(f"Progression Élo — {home} vs {away}", color="#e8edf2", fontsize=13)
    ax.set_ylabel("Élo", color="#e8edf2")
    ax.tick_params(colors="#8a99ad")
    for spine in ax.spines.values():
        spine.set_color("#2a3b52")
    ax.legend(facecolor="#12233a", labelcolor="#e8edf2", edgecolor="#2a3b52")
    ax.grid(True, alpha=0.15, color="#8a99ad")
    fig.autofmt_xdate()

    path = os.path.join(STATE_DIR, "elo_chart_web.png")
    fig.savefig(path, dpi=130, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return send_from_directory(STATE_DIR, "elo_chart_web.png")


@app.route("/api/odds/<code>")
def api_odds(code):
    code = code.upper()
    if not ODDS_API_KEY:
        return jsonify({"error": "ODDS_API_KEY non configurée"}), 503
    if code not in COMPETITIONS:
        return jsonify({"error": "Code inconnu"}), 404
    if code not in ODDS_SPORT_KEYS:
        return jsonify({"error": "Cotes non disponibles pour cette compétition"}), 404
    return jsonify({"competition": COMPETITIONS[code], "odds": get_odds_snapshot(code)})


@app.route("/api/value/<code>")
def api_value(code):
    code = code.upper()
    if not ODDS_API_KEY:
        return jsonify({"error": "ODDS_API_KEY non configurée"}), 503
    if code not in COMPETITIONS:
        return jsonify({"error": "Code inconnu"}), 404
    try:
        return jsonify({"competition": COMPETITIONS[code], "value_bets": find_value_bets(code)})
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/live/<code>")
def api_live(code):
    code = code.upper()
    if code not in COMPETITIONS:
        return jsonify({"error": "Code inconnu"}), 404
    try:
        return jsonify({"competition": COMPETITIONS[code], "matches": get_live_snapshot(code)})
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/status")
def api_status():
    return jsonify({
        "football_data": bool(FOOTBALL_API_KEY),
        "api_football": bool(API_FOOTBALL_KEY),
        "odds_api": bool(ODDS_API_KEY),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
