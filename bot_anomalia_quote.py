import os
import time
import re
import unicodedata
import logging
import requests
from collections import deque

# =========================
# Logging
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("quote-jump-bot")

# =========================
# Environment
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID        = os.getenv("CHAT_ID", "")

RAPIDAPI_KEY   = os.getenv("RAPIDAPI_KEY", "")
RAPIDAPI_HOST  = os.getenv("RAPIDAPI_HOST", "soccer-football-info.p.rapidapi.com")
RAPIDAPI_BASE  = f"https://{RAPIDAPI_HOST}"

# Endpoint
RAPIDAPI_LIVE_FULL_PATH = "/live/full/"
RAPIDAPI_LIVE_PARAMS = {"i": "en_US", "f": "json", "e": "no"}

# Business rules - OTTIMIZZATI
MIN_RISE        = float(os.getenv("MIN_RISE", "0.03"))
MAX_RISE        = float(os.getenv("MAX_RISE", "0.30"))  # 🆕 SOGLIA MASSIMA per evitare falsi segnali!
BASELINE_MIN    = float(os.getenv("BASELINE_MIN", "1.30"))
BASELINE_MAX    = float(os.getenv("BASELINE_MAX", "1.90"))
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL_SECONDS", "4"))
WAIT_AFTER_GOAL_SEC = int(os.getenv("WAIT_AFTER_GOAL_SEC", "20"))

# 🆕 MINUTO SOGLIA per tipo notifica
MINUTE_THRESHOLD_HT = int(os.getenv("MINUTE_THRESHOLD_HT", "25"))  # ≤25' = Over 1.5 HT

# Baseline sampling
BASELINE_SAMPLES = int(os.getenv("BASELINE_SAMPLES", "2"))
BASELINE_SAMPLE_INTERVAL = int(os.getenv("BASELINE_SAMPLE_INTERVAL", "6"))

# Rate limiting
MAX_API_CALLS_PER_LOOP = int(os.getenv("MAX_API_CALLS_PER_LOOP", "10"))
API_CALL_MIN_GAP_MS = int(os.getenv("API_CALL_MIN_GAP_MS", "300"))
_last_api_call_ts_ms = 0

# Priorità goal recenti
RECENT_GOAL_PRIORITY_SEC = 120

COOLDOWN_ON_DAILY_429_MIN = int(os.getenv("COOLDOWN_ON_DAILY_429_MIN", "30"))
_last_daily_429_ts = 0

# API retry
MAX_API_RETRIES = 2
API_RETRY_DELAY = 1

# FILTRI LEGHE
ENABLE_LEAGUE_FILTER = os.getenv("ENABLE_LEAGUE_FILTER", "true").lower() == "true"

# Esclude SOLO altri sport (non calcio)
# TUTTO IL CALCIO È INCLUSO: senior, giovanili, femminile, riserve, etc.
LEAGUE_EXCLUDE_KEYWORDS = [
    # eSports/Virtual (NON calcio reale)
    "esoccer", "e-soccer", "e soccer",
    "cyber", "e-football", 
    "esports", "e-sports",
    "fifa", "pes", "efootball",
    "virtual", "simulated",
    "gtworld", "baller",
    
    # Partite non standard (troppo brevi)
    "6 mins", "8 mins", "10 mins", "12 mins", 
    "15 mins", "20 mins", "30 mins",
    
    # Altri sport
    "h2h gg",  # Head to head games
]

# Se vuoi escludere anche giovanili/riserve, imposta questa variabile
EXCLUDE_YOUTH = os.getenv("EXCLUDE_YOUTH", "false").lower() == "true"
if EXCLUDE_YOUTH:
    LEAGUE_EXCLUDE_KEYWORDS.extend(["u19", "u23", "u21", "u20", "u18", "u17", "reserve", "riserve", "primavera"])

HEADERS = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPIDAPI_HOST}

# =========================
# Stato match
# =========================
class MatchState:
    __slots__ = ("first_seen_at", "first_seen_score", "goal_time", "goal_minute",
                 "scoring_team", "baseline_samples", "baseline", "last_quote", 
                 "notified", "notified_ht", "ht_score_1_0_or_0_1", "tries", 
                 "last_check", "consecutive_errors", "last_seen_loop", 
                 "last_seen_minute", "score_stable_count")
    
    def __init__(self):
        self.first_seen_at = time.time()
        self.first_seen_score = None
        self.goal_time = None
        self.goal_minute = None
        self.scoring_team = None
        self.baseline_samples = deque(maxlen=BASELINE_SAMPLES)
        self.baseline = None
        self.last_quote = None
        self.notified = False  # Prima notifica (main)
        self.notified_ht = False  # 🆕 Notifica HT->FT
        self.ht_score_1_0_or_0_1 = False  # 🆕 Flag HT con 1-0 o 0-1
        self.tries = 0
        self.last_check = 0
        self.consecutive_errors = 0
        self.last_seen_loop = 0
        self.last_seen_minute = 0
        self.score_stable_count = 0  # 🆕 Conta quante volte vediamo lo stesso score

match_state = {}
_loop = 0

# =========================
# Helpers
# =========================
def send_telegram_message(message: str) -> bool:
    """Invia messaggio Telegram con retry"""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("⚠️ Telegram non configurato")
        return False
    
    for attempt in range(2):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            r = requests.post(
                url, 
                data={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, 
                timeout=10
            )
            if r and r.ok:
                return True
        except Exception as e:
            logger.warning("Telegram attempt %d error: %s", attempt + 1, e)
        
        if attempt < 1:
            time.sleep(0.5)
    
    return False

def http_get(url, headers=None, params=None, timeout=15, retries=MAX_API_RETRIES):
    """HTTP GET con retry automatico"""
    global _last_daily_429_ts
    
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=timeout)
            
            if r.status_code == 429:
                if "daily" in (r.text or "").lower():
                    _last_daily_429_ts = int(time.time())
                    logger.error("❌ DAILY QUOTA REACHED")
                    return None
                if attempt < retries - 1:
                    time.sleep(API_RETRY_DELAY)
                    continue
            
            if r.ok:
                return r
            
            if attempt < retries - 1:
                time.sleep(API_RETRY_DELAY)
                
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(API_RETRY_DELAY)
    
    return None

def parse_score_tuple(score_home: str, score_away: str) -> tuple:
    """Parse score da stringhe separate"""
    try:
        h = int(score_home) if score_home and score_home.isdigit() else 0
        a = int(score_away) if score_away and score_away.isdigit() else 0
        return (h, a)
    except:
        return (0, 0)

def parse_timer_to_minutes(timer: str) -> int:
    """Converte timer (es: '35:57' o '45:00+02:30') in minuti totali"""
    try:
        if not timer:
            return 0
        # Gestisce "45:00+02:30" -> prende 45
        timer = timer.split('+')[0].strip()
        parts = timer.split(':')
        if len(parts) >= 2:
            return int(parts[0])
        return 0
    except:
        return 0

def is_halftime_or_fulltime(timer: str) -> bool:
    """Verifica se siamo a HT o FT"""
    if not timer:
        return False
    timer_lower = timer.lower()
    # Segnali di HT/FT
    if any(x in timer_lower for x in ["ht", "ft", "half", "full", "interval", "ended"]):
        return True
    # Minuto 45 con supplementari
    if timer.startswith("45:00+") or timer.startswith("90:00+"):
        return True
    return False

def is_excluded_league(league_name: str) -> bool:
    """Verifica se la lega è da escludere"""
    league_lower = league_name.lower()
    for keyword in LEAGUE_EXCLUDE_KEYWORDS:
        if keyword.lower() in league_lower:
            return True
    return False

def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))

def norm_name(s: str) -> str:
    s = strip_accents(s).lower()
    s = re.sub(r"[''`]", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())

def create_match_signature(home: str, away: str, league: str) -> str:
    """Crea firma univoca per match"""
    return f"{norm_name(home)}|{norm_name(away)}|{norm_name(league)}"

def parse_price(x):
    """Parsing quote"""
    if x is None or x == "-":
        return None
    if isinstance(x, (int, float)):
        val = float(x)
        return val if 1.01 <= val <= 1000 else None
    
    s = str(x).strip()
    try:
        val = float(s.replace(",", "."))
        return val if 1.01 <= val <= 1000 else None
    except:
        return None

def can_call_api():
    global _last_api_call_ts_ms
    now_ms = int(time.time() * 1000)
    return (now_ms - _last_api_call_ts_ms) >= API_CALL_MIN_GAP_MS

def mark_api_call():
    global _last_api_call_ts_ms
    _last_api_call_ts_ms = int(time.time() * 1000)

# =========================
# API
# =========================
def get_live_matches_with_odds():
    """
    Recupera TUTTI i dati in una singola chiamata:
    - Match live
    - Punteggi
    - Minuti
    - Quote 1X2
    """
    url = f"{RAPIDAPI_BASE.rstrip('/')}/{RAPIDAPI_LIVE_FULL_PATH.lstrip('/')}"
    r = http_get(url, headers=HEADERS, params=RAPIDAPI_LIVE_PARAMS, timeout=20)
    
    if not r or not r.ok:
        return []

    try:
        data = r.json() or {}
    except Exception as e:
        logger.error("JSON parse error: %s", e)
        return []

    raw_events = data.get("result") or []
    events = []
    seen_signatures = set()
    
    # 🆕 Contatori per debug
    total_raw = len(raw_events)
    filtered_not_in_play = 0
    filtered_excluded_league = 0
    filtered_no_odds = 0
    filtered_duplicate = 0

    for match in raw_events:
        # Estrai dati base
        event_id = str(match.get("id", ""))
        timer = match.get("timer", "")
        in_play = match.get("in_play", False)
        
        # Se non in play, salta
        if not in_play:
            filtered_not_in_play += 1
            continue
        
        # Championship
        champ = match.get("championship") or {}
        league = champ.get("name", "").strip()
        
        if not league or (ENABLE_LEAGUE_FILTER and is_excluded_league(league)):
            if league and ENABLE_LEAGUE_FILTER:
                filtered_excluded_league += 1
            continue

        # Teams
        team_a = match.get("teamA") or {}
        team_b = match.get("teamB") or {}
        
        home = team_a.get("name", "").strip()
        away = team_b.get("name", "").strip()
        
        if not home or not away or not event_id:
            continue

        # Score
        score_a = team_a.get("score") or {}
        score_b = team_b.get("score") or {}
        
        score_home = score_a.get("f", "0")
        score_away = score_b.get("f", "0")
        
        # Score 1H (per controllo HT)
        score_1h_home = score_a.get("1h")
        score_1h_away = score_b.get("1h")
        
        cur_score = parse_score_tuple(score_home, score_away)
        
        # Minuto
        current_minute = parse_timer_to_minutes(timer)

        # Quote LIVE
        odds_data = match.get("odds") or {}
        live_odds = odds_data.get("live") or {}
        odds_1x2 = live_odds.get("1X2") or {}
        bet365_odds = odds_1x2.get("bet365") or {}
        
        home_price = parse_price(bet365_odds.get("1"))
        draw_price = parse_price(bet365_odds.get("X"))
        away_price = parse_price(bet365_odds.get("2"))

        # Signature
        signature = create_match_signature(home, away, league)
        if signature in seen_signatures:
            filtered_duplicate += 1
            continue
        
        seen_signatures.add(signature)
        
        # 🆕 Se non ci sono quote, logga e salta
        if home_price is None and away_price is None:
            filtered_no_odds += 1
            continue

        events.append({
            "id": event_id,
            "home": home,
            "away": away,
            "league": league,
            "score": cur_score,
            "score_1h": (score_1h_home, score_1h_away),
            "timer": timer,
            "minute": current_minute,
            "signature": signature,
            "odds": {
                "home": home_price,
                "draw": draw_price,
                "away": away_price
            }
        })

    # 🆕 Log dettagliato
    if total_raw > 0 and _loop % 30 == 1:
        logger.info("🔍 API Filter Stats: %d total → %d accepted", total_raw, len(events))
        if filtered_not_in_play:
            logger.info("   • %d not in play", filtered_not_in_play)
        if filtered_excluded_league:
            logger.info("   • %d excluded leagues", filtered_excluded_league)
        if filtered_no_odds:
            logger.info("   • %d no odds available", filtered_no_odds)
        if filtered_duplicate:
            logger.info("   • %d duplicates", filtered_duplicate)

    return events

# =========================
# Main Loop - SUPER OTTIMIZZATO
# =========================
def main_loop():
    global _last_daily_429_ts, _loop

    while True:
        try:
            # Controllo daily quota
            if _last_daily_429_ts:
                elapsed = int(time.time()) - _last_daily_429_ts
                if elapsed < COOLDOWN_ON_DAILY_429_MIN * 60:
                    remaining = (COOLDOWN_ON_DAILY_429_MIN * 60 - elapsed) // 60
                    if _loop % 20 == 0:
                        logger.info("⏳ Cooldown: %d min", remaining)
                    time.sleep(CHECK_INTERVAL)
                    continue
                _last_daily_429_ts = 0
                logger.info("✅ Cooldown terminato")

            # Rate limiting
            if not can_call_api():
                time.sleep(0.5)
                continue

            # UNA SOLA CHIAMATA API!
            live = get_live_matches_with_odds()
            mark_api_call()
            
            if not live:
                time.sleep(CHECK_INTERVAL)
                continue

            _loop += 1
            
            # Segna match presenti
            current_match_ids = {m["id"] for m in live}
            for eid in current_match_ids:
                if eid in match_state:
                    match_state[eid].last_seen_loop = _loop

            if _loop % 30 == 1:
                logger.info("📊 %d live | %d monitored", len(live), len(match_state))

            now = time.time()

            # PRIORITÀ: Match con goal recente (ultimi 2 minuti)
            prioritized_matches = []
            other_matches = []
            
            for match in live:
                eid = match["id"]
                if eid in match_state:
                    st = match_state[eid]
                    if st.goal_time and (now - st.goal_time) < RECENT_GOAL_PRIORITY_SEC:
                        prioritized_matches.append(match)
                        continue
                other_matches.append(match)
            
            # Processa prima i prioritari, poi gli altri
            all_matches = prioritized_matches + other_matches

            for match in all_matches:
                eid = match["id"]
                home = match["home"]
                away = match["away"]
                league = match["league"]
                cur_score = match["score"]
                score_1h = match["score_1h"]
                timer = match["timer"]
                current_minute = match["minute"]
                odds = match["odds"]

                # Inizializza stato
                if eid not in match_state:
                    match_state[eid] = MatchState()
                    match_state[eid].first_seen_score = cur_score
                    match_state[eid].last_seen_loop = _loop
                    match_state[eid].last_seen_minute = current_minute

                st = match_state[eid]
                st.last_seen_loop = _loop

                # 🆕 VALIDAZIONE SCORE: deve essere stabile per almeno 2 loop
                if st.first_seen_score is not None:
                    if cur_score == st.first_seen_score:
                        st.score_stable_count += 1
                    else:
                        # Score cambiato, reset
                        st.first_seen_score = cur_score
                        st.score_stable_count = 1

                # 🆕 CONTROLLO HT->FT RE-NOTIFICA
                # Se abbiamo già mandato notifica HT e ora siamo nel 2° tempo con 1-0 o 0-1
                if st.notified and not st.notified_ht and current_minute > 45:
                    # Verifica score HT
                    if score_1h[0] is not None and score_1h[1] is not None:
                        ht_score_tuple = parse_score_tuple(str(score_1h[0]), str(score_1h[1]))
                        if ht_score_tuple in [(1, 0), (0, 1)]:
                            st.ht_score_1_0_or_0_1 = True
                    
                    # Se HT era 1-0 o 0-1, manda notifica FT
                    if st.ht_score_1_0_or_0_1:
                        team_name = home if st.scoring_team == "home" else away
                        team_label = "1" if st.scoring_team == "home" else "2"
                        
                        msg = (
                            f"🔄 <b>AGGIORNAMENTO</b> 🔄\n\n"
                            f"🏆 {league}\n"
                            f"⚽ <b>{home}</b> vs <b>{away}</b>\n"
                            f"📊 HT: <b>{ht_score_tuple[0]}-{ht_score_tuple[1]}</b>\n"
                            f"⏱ Ora: {current_minute}'\n\n"
                            f"✅ Primo tempo finito {ht_score_tuple[0]}-{ht_score_tuple[1]}\n"
                            f"💡 Team {team_label} ({team_name}) ancora in vantaggio\n\n"
                            f"🎯 <b>GIOCA: OVER 2.5 FINALE</b> 🎯"
                        )
                        
                        if send_telegram_message(msg):
                            logger.info("🔄 RE-NOTIFICA FT: %s vs %s (HT era %d-%d)", 
                                       home, away, ht_score_tuple[0], ht_score_tuple[1])
                        
                        st.notified_ht = True

                # STEP 1: Rileva goal (solo se score stabile)
                if st.goal_time is None and st.score_stable_count >= 2:
                    first_score = st.first_seen_score or (0, 0)
                    
                    if first_score != (0, 0):
                        continue
                    
                    if cur_score == (1, 0):
                        st.goal_time = now
                        st.goal_minute = current_minute
                        st.scoring_team = "home"
                        logger.info("⚽ GOAL %d': %s vs %s (1-0) | %s", 
                                   current_minute, home, away, league)
                        continue
                    elif cur_score == (0, 1):
                        st.goal_time = now
                        st.goal_minute = current_minute
                        st.scoring_team = "away"
                        logger.info("⚽ GOAL %d': %s vs %s (0-1) | %s", 
                                   current_minute, home, away, league)
                        continue
                    else:
                        continue

                # STEP 2: Verifica che abbiamo rilevato un goal
                if st.goal_time is None:
                    continue

                # STEP 3: Verifica score (deve essere ancora 1-0 o 0-1)
                expected = (1, 0) if st.scoring_team == "home" else (0, 1)
                if cur_score != expected:
                    # Score cambiato! Forse 1-1 o altro goal
                    if not st.notified:
                        logger.warning("⚠️ Score cambiato: %s vs %s (%d-%d, expected %d-%d)", 
                                      home, away, cur_score[0], cur_score[1], expected[0], expected[1])
                    continue

                if st.notified:
                    continue

                # STEP 4: Attesa post-goal
                if now - st.goal_time < WAIT_AFTER_GOAL_SEC:
                    continue

                # STEP 5: Throttling per match
                if now - st.last_check < BASELINE_SAMPLE_INTERVAL:
                    continue

                st.tries += 1
                st.last_check = now

                # Le quote sono già disponibili!
                scorer_price = odds["home"] if st.scoring_team == "home" else odds["away"]
                
                if scorer_price is None:
                    st.consecutive_errors += 1
                    if st.consecutive_errors > 8:
                        logger.warning("⚠️ Skip %s vs %s (no odds)", home, away)
                        st.notified = True
                    continue

                st.consecutive_errors = 0

                # STEP 6: BASELINE
                if st.baseline is None:
                    if scorer_price < BASELINE_MIN or scorer_price > BASELINE_MAX:
                        logger.info("❌ %.2f fuori range: %s vs %s", scorer_price, home, away)
                        st.notified = True
                        continue
                    
                    st.baseline_samples.append(scorer_price)
                    
                    if len(st.baseline_samples) >= BASELINE_SAMPLES:
                        st.baseline = min(st.baseline_samples)
                        logger.info("✅ Baseline %.2f (%d'): %s vs %s", 
                                   st.baseline, current_minute, home, away)
                    else:
                        logger.info("📊 Sample %d/%d: %.2f (%d') | %s vs %s", 
                                   len(st.baseline_samples), BASELINE_SAMPLES, 
                                   scorer_price, current_minute, home, away)
                    
                    st.last_quote = scorer_price
                    continue

                # STEP 7: Monitora
                delta = scorer_price - st.baseline
                st.last_quote = scorer_price

                # Log variazioni anche se non trigger alert
                if delta >= MIN_RISE * 0.7:
                    logger.info("📈 %d' | %s vs %s: %.2f (base %.2f, Δ+%.3f)", 
                               current_minute, home, away, scorer_price, st.baseline, delta)

                # STEP 8: Alert con VALIDAZIONE SOGLIA MASSIMA 🆕
                if delta >= MIN_RISE and delta <= MAX_RISE:  # 🆕 Controllo MAX!
                    team_name = home if st.scoring_team == "home" else away
                    team_label = "1" if st.scoring_team == "home" else "2"
                    pct = (delta / st.baseline * 100)
                    
                    # 🆕 TIPO DI NOTIFICA BASATO SUL MINUTO
                    if st.goal_minute <= MINUTE_THRESHOLD_HT:
                        bet_type = "OVER 1.5 PRIMO TEMPO"
                        emoji = "⏰"
                    else:
                        bet_type = "OVER 2.5 FINALE"
                        emoji = "🎯"
                    
                    msg = (
                        f"💰💎 <b>QUOTE JUMP</b> 💎💰\n\n"
                        f"🏆 {league}\n"
                        f"⚽ <b>{home}</b> vs <b>{away}</b>\n"
                        f"📊 <b>{cur_score[0]}-{cur_score[1]}</b> ({current_minute}')\n\n"
                        f"⚽ Goal al {st.goal_minute}'\n"
                        f"💸 Quota <b>{team_label}</b> ({team_name}):\n"
                        f"<b>{st.baseline:.2f}</b> → <b>{scorer_price:.2f}</b>\n"
                        f"📈 <b>+{delta:.2f}</b> (+{pct:.1f}%)\n\n"
                        f"{emoji} <b>GIOCA: {bet_type}</b> {emoji}"
                    )
                    
                    if send_telegram_message(msg):
                        logger.info("✅ ALERT %d' [%s]: %s vs %s | %.2f→%.2f (+%.2f)", 
                                   current_minute, bet_type, home, away, 
                                   st.baseline, scorer_price, delta)
                    
                    st.notified = True
                
                # 🆕 Se delta troppo alto, logga e salta
                elif delta > MAX_RISE:
                    logger.warning("⚠️ Delta troppo alto (+%.2f > %.2f): %s vs %s - POSSIBILE FALSO SEGNALE", 
                                  delta, MAX_RISE, home, away)
                    st.notified = True  # Blocca per evitare spam

            # Pulizia aggressiva
            to_remove = []
            for k, v in match_state.items():
                age = now - v.first_seen_at
                loops_ago = _loop - v.last_seen_loop
                
                if loops_ago > 2 or age > 7200:
                    to_remove.append(k)
            
            if to_remove:
                for k in to_remove:
                    del match_state[k]

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            logger.info("🛑 Stop")
            break
        except Exception as e:
            logger.exception("❌ Error: %s", e)
            time.sleep(5)

# =========================
# Start
# =========================
def main():
    if not all([TELEGRAM_TOKEN, CHAT_ID, RAPIDAPI_KEY]):
        raise SystemExit("❌ Variabili mancanti")
    
    logger.info("="*60)
    logger.info("🚀 BOT QUOTE JUMP - SUPER OPTIMIZED v3.0")
    logger.info("="*60)
    logger.info("⚙️  Config:")
    logger.info("   • API: %s", RAPIDAPI_HOST)
    logger.info("   • Min rise: +%.2f | Max rise: +%.2f", MIN_RISE, MAX_RISE)
    logger.info("   • Range: %.2f-%.2f", BASELINE_MIN, BASELINE_MAX)
    logger.info("   • Wait goal: %ds", WAIT_AFTER_GOAL_SEC)
    logger.info("   • Minute HT threshold: ≤%d'", MINUTE_THRESHOLD_HT)
    logger.info("   • Check: %ds", CHECK_INTERVAL)
    logger.info("   • Samples: %d (ogni %ds)", BASELINE_SAMPLES, BASELINE_SAMPLE_INTERVAL)
    logger.info("   • League filter: %s", "ON" if ENABLE_LEAGUE_FILTER else "OFF (monitoring ALL)")
    logger.info("="*60)
    
    send_telegram_message(
        f"🤖 <b>Bot QUOTE JUMP v3.0</b> ⚡\n\n"
        f"⚽ <b>TUTTO IL CALCIO LIVE</b>\n"
        f"✅ Senior, U19/U23, Femminile, Reserve\n"
        f"✅ Tutte le leghe mondiali\n"
        f"❌ Solo eSports/Virtual esclusi\n\n"
        f"📊 Quote {BASELINE_MIN:.2f}-{BASELINE_MAX:.2f}\n"
        f"📈 Rise: <b>+{MIN_RISE:.2f}</b> to <b>+{MAX_RISE:.2f}</b>\n"
        f"⚡ Wait <b>{WAIT_AFTER_GOAL_SEC}s</b> post-goal\n\n"
        f"🎯 <b>DUAL MODE:</b>\n"
        f"⏰ Goal ≤{MINUTE_THRESHOLD_HT}' → <b>OVER 1.5 HT</b>\n"
        f"🎯 Goal >{MINUTE_THRESHOLD_HT}' → <b>OVER 2.5 FT</b>\n\n"
        f"🔄 <b>Auto re-notify se HT 1-0/0-1</b>\n\n"
        f"🔍 Monitoraggio attivo!"
    )
    
    main_loop()

if __name__ == "__main__":
    main()
