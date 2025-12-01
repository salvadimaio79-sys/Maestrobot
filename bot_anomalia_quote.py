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

# Business rules
MIN_RISE        = float(os.getenv("MIN_RISE", "0.06"))
MAX_RISE        = float(os.getenv("MAX_RISE", "0.70"))
BASELINE_MIN    = float(os.getenv("BASELINE_MIN", "1.30"))  # 🔥 Baseline 1.30
BASELINE_MAX    = float(os.getenv("BASELINE_MAX", "1.75"))
MAX_FINAL_QUOTE = float(os.getenv("MAX_FINAL_QUOTE", "2.00"))  # 🔥 Max quota aumentata 2.00
MIN_OVER25_QUOTE = float(os.getenv("MIN_OVER25_QUOTE", "1.50"))  # 🔥 Over 2.5 min 1.50
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL_SECONDS", "4"))
WAIT_AFTER_GOAL_SEC = int(os.getenv("WAIT_AFTER_GOAL_SEC", "10"))

# Baseline sampling
BASELINE_SAMPLES = int(os.getenv("BASELINE_SAMPLES", "2"))
BASELINE_SAMPLE_INTERVAL = int(os.getenv("BASELINE_SAMPLE_INTERVAL", "4"))

# 🔥 DUE FINESTRE GOAL:
# 25-60' → OVER 2.5 FINALE (€50)
# 60-80' → OVER 1.5 FINALE (€50)
GOAL_WINDOW_1_MIN = int(os.getenv("GOAL_WINDOW_1_MIN", "25"))
GOAL_WINDOW_1_MAX = int(os.getenv("GOAL_WINDOW_1_MAX", "60"))
GOAL_WINDOW_2_MIN = int(os.getenv("GOAL_WINDOW_2_MIN", "60"))
GOAL_WINDOW_2_MAX = int(os.getenv("GOAL_WINDOW_2_MAX", "80"))

# 🔥 Stake
STAKE = int(os.getenv("STAKE", "50"))  # €50 per entrambi

# Rate limiting
MAX_API_RETRIES = 2
API_RETRY_DELAY = 1
COOLDOWN_ON_DAILY_429_MIN = int(os.getenv("COOLDOWN_ON_DAILY_429_MIN", "30"))
_last_daily_429_ts = 0

# 🆕 STATISTICHE GIORNALIERE
ENABLE_DAILY_STATS = os.getenv("ENABLE_DAILY_STATS", "true").lower() == "true"
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "0"))  # 🔥 Report a mezzanotte (00:00)

class DailyStats:
    """Statistiche giornaliere"""
    def __init__(self):
        self.reset_date = time.strftime("%Y-%m-%d")
        self.signals_sent = []  # Lista di segnali inviati
        self.total_sent = 0
        self.total_won = 0
        self.total_lost = 0
        self.total_pending = 0
        self.last_report_sent = None
    
    def add_signal(self, match_id, home, away, league, bet_type, goal_minute, 
                   baseline, final_quote, delta, timestamp):
        """Aggiungi un segnale"""
        signal = {
            "id": match_id,
            "home": home,
            "away": away,
            "league": league,
            "bet_type": bet_type,
            "goal_minute": goal_minute,
            "baseline": baseline,
            "final_quote": final_quote,
            "delta": delta,
            "timestamp": timestamp,
            "status": "pending",  # pending, won, lost
            "final_score": None,
            "checked_at": None
        }
        self.signals_sent.append(signal)
        self.total_sent += 1
        self.total_pending += 1
    
    def update_signal(self, match_id, final_score, status):
        """Aggiorna risultato di un segnale"""
        for signal in self.signals_sent:
            if signal["id"] == match_id and signal["status"] == "pending":
                signal["status"] = status
                signal["final_score"] = final_score
                signal["checked_at"] = time.time()
                
                self.total_pending -= 1
                if status == "won":
                    self.total_won += 1
                elif status == "lost":
                    self.total_lost += 1
                break
    
    def check_if_need_reset(self):
        """Controlla se è un nuovo giorno"""
        today = time.strftime("%Y-%m-%d")
        if today != self.reset_date:
            return True
        return False
    
    def reset(self):
        """Reset statistiche per nuovo giorno"""
        self.reset_date = time.strftime("%Y-%m-%d")
        self.signals_sent = []
        self.total_sent = 0
        self.total_won = 0
        self.total_lost = 0
        self.total_pending = 0

daily_stats = DailyStats()

# FILTRI LEGHE - Solo eSports
LEAGUE_EXCLUDE_KEYWORDS = [
    "esoccer", "e-soccer", "cyber", "e-football", 
    "esports", "fifa", "pes", "efootball",
    "virtual", "simulated", "gtworld", "baller",
    "6 mins", "8 mins", "10 mins", "12 mins", 
    "15 mins", "20 mins", "30 mins", "h2h gg",
]

def is_excluded_league(league_name: str) -> bool:
    """Verifica se la lega è da escludere (solo eSports/Virtual)"""
    league_lower = league_name.lower()
    for keyword in LEAGUE_EXCLUDE_KEYWORDS:
        if keyword.lower() in league_lower:
            return True
    return False

HEADERS = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPIDAPI_HOST}

# =========================
# Stato match - SEMPLICE
# =========================
class MatchState:
    __slots__ = ("first_seen_at", "first_seen_score", "goal_time", "goal_minute",
                 "scoring_team", "baseline_samples", "baseline", "last_quote", 
                 "notified", "tries", "last_check", "consecutive_errors",
                 "pending_goal_score", "pending_goal_count", "pre_goal_quote",
                 "red_card_detected")
    
    def __init__(self):
        self.first_seen_at = time.time()
        self.first_seen_score = None
        self.goal_time = None
        self.goal_minute = None
        self.scoring_team = None
        self.baseline_samples = deque(maxlen=BASELINE_SAMPLES)
        self.baseline = None
        self.last_quote = None
        self.notified = False
        self.tries = 0
        self.last_check = 0
        self.consecutive_errors = 0
        self.pending_goal_score = None
        self.pending_goal_count = 0
        self.pre_goal_quote = None
        self.red_card_detected = False  # 🔥 Cartellino rosso rilevato

match_state = {}
_loop = 0

# =========================
# Helpers
# =========================
def send_telegram_message(message: str) -> bool:
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
    try:
        h = int(score_home) if score_home and score_home.isdigit() else 0
        a = int(score_away) if score_away and score_away.isdigit() else 0
        return (h, a)
    except:
        return (0, 0)

def parse_timer_to_minutes(timer: str) -> int:
    try:
        if not timer:
            return 0
        timer = timer.split('+')[0].strip()
        parts = timer.split(':')
        if len(parts) >= 2:
            return int(parts[0])
        return 0
    except:
        return 0

def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))

def norm_name(s: str) -> str:
    s = strip_accents(s).lower()
    s = re.sub(r"[''`]", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())

def create_match_signature(home: str, away: str, league: str) -> str:
    return f"{norm_name(home)}|{norm_name(away)}|{norm_name(league)}"

def parse_price(x):
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

def check_signal_result(signal, current_score, ht_score, current_minute):
    """
    Verifica se un segnale è vincente o perdente
    Returns: 'won', 'lost', or 'pending'
    """
    if signal["status"] != "pending":
        return signal["status"]
    
    bet_type = signal.get("bet_type", "")
    
    # Partita non finita
    if current_minute < 90:
        return "pending"
    
    final_total = current_score[0] + current_score[1]
    
    # OVER 2.5 FINALE
    if "2.5" in bet_type:
        if final_total >= 3:
            return "won"
        else:
            return "lost"
    
    # OVER 1.5 FINALE
    elif "1.5" in bet_type:
        if final_total >= 2:
            return "won"
        else:
            return "lost"
    
    return "pending"

def send_daily_report():
    """Invia report giornaliero pulito"""
    if not ENABLE_DAILY_STATS:
        return
    
    if daily_stats.total_sent == 0:
        # Nessun segnale inviato oggi
        msg = (
            f"📊 <b>REPORT GIORNALIERO</b> 📊\n"
            f"📅 {daily_stats.reset_date}\n\n"
            f"📭 Nessun segnale inviato oggi\n\n"
            f"🔄 Statistiche resettate"
        )
        send_telegram_message(msg)
        logger.info("📊 Report giornaliero: 0 segnali")
        daily_stats.last_report_sent = time.time()
        return
    
    win_rate = (daily_stats.total_won / daily_stats.total_sent * 100) if daily_stats.total_sent > 0 else 0
    
    # Calcola profitto/perdita stimato
    profit_won = daily_stats.total_won * STAKE * 0.4  # ~40% rendimento medio
    loss_total = daily_stats.total_lost * STAKE
    net_profit = profit_won - loss_total
    
    msg = (
        f"📊 <b>REPORT GIORNALIERO</b> 📊\n"
        f"📅 {daily_stats.reset_date}\n\n"
        f"📨 Segnali: <b>{daily_stats.total_sent}</b>\n"
        f"✅ Vinti: <b>{daily_stats.total_won}</b>\n"
        f"❌ Persi: <b>{daily_stats.total_lost}</b>\n"
        f"⏳ Pending: <b>{daily_stats.total_pending}</b>\n\n"
        f"📈 Win Rate: <b>{win_rate:.1f}%</b>\n"
        f"💰 Profitto stimato: <b>€{net_profit:+.0f}</b>\n\n"
    )
    
    # Lista dettagli vincenti (max 10)
    won_signals = [s for s in daily_stats.signals_sent if s["status"] == "won"]
    if won_signals:
        msg += "✅ <b>VINCENTI:</b>\n"
        for i, signal in enumerate(won_signals[:10], 1):
            score = signal.get("final_score", (0, 0))
            msg += f"{i}. {signal['home']} vs {signal['away']} ({score[0]}-{score[1]})\n"
        if len(won_signals) > 10:
            msg += f"   ... e altri {len(won_signals) - 10}\n"
        msg += "\n"
    
    # Lista dettagli perdenti (max 10)
    lost_signals = [s for s in daily_stats.signals_sent if s["status"] == "lost"]
    if lost_signals:
        msg += "❌ <b>PERDENTI:</b>\n"
        for i, signal in enumerate(lost_signals[:10], 1):
            score = signal.get("final_score", (0, 0))
            msg += f"{i}. {signal['home']} vs {signal['away']} ({score[0]}-{score[1]})\n"
        if len(lost_signals) > 10:
            msg += f"   ... e altri {len(lost_signals) - 10}\n"
        msg += "\n"
    
    msg += "🔄 Statistiche resettate per domani"
    
    send_telegram_message(msg)
    logger.info("📊 Report: %d segnali | %d vinti | %d persi | %.1f%% WR", 
               daily_stats.total_sent, daily_stats.total_won, 
               daily_stats.total_lost, win_rate)
    daily_stats.last_report_sent = time.time()

# =========================
# API
# =========================
def get_live_matches_with_odds():
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

    for match in raw_events:
        event_id = str(match.get("id", ""))
        in_play = match.get("in_play", False)
        
        if not in_play:
            continue
        
        champ = match.get("championship") or {}
        league = champ.get("name", "").strip()
        
        if not league or is_excluded_league(league):
            continue

        team_a = match.get("teamA") or {}
        team_b = match.get("teamB") or {}
        
        home = team_a.get("name", "").strip()
        away = team_b.get("name", "").strip()
        
        if not home or not away or not event_id:
            continue

        score_a = team_a.get("score") or {}
        score_b = team_b.get("score") or {}
        
        score_home = score_a.get("f", "0")
        score_away = score_b.get("f", "0")
        
        # 🆕 Score primo tempo
        score_1h_home = score_a.get("1h")
        score_1h_away = score_b.get("1h")
        
        cur_score = parse_score_tuple(score_home, score_away)
        ht_score = None
        if score_1h_home is not None and score_1h_away is not None:
            ht_score = parse_score_tuple(str(score_1h_home), str(score_1h_away))
        
        timer = match.get("timer", "")
        current_minute = parse_timer_to_minutes(timer)
        
        # 🔥 Rilevamento cartellino rosso
        red_card_home = score_a.get("rc", 0) or 0
        red_card_away = score_b.get("rc", 0) or 0
        has_red_card = (red_card_home > 0) or (red_card_away > 0)

        odds_data = match.get("odds") or {}
        live_odds = odds_data.get("live") or {}
        odds_1x2 = live_odds.get("1X2") or {}
        bet365_odds = odds_1x2.get("bet365") or {}
        
        home_price = parse_price(bet365_odds.get("1"))
        away_price = parse_price(bet365_odds.get("2"))

        signature = create_match_signature(home, away, league)
        if signature in seen_signatures:
            continue
        
        seen_signatures.add(signature)

        events.append({
            "id": event_id,
            "home": home,
            "away": away,
            "league": league,
            "score": cur_score,
            "ht_score": ht_score,
            "timer": timer,
            "minute": current_minute,
            "signature": signature,
            "home_price": home_price,
            "away_price": away_price,
            "has_red_card": has_red_card  # 🔥 Flag cartellino rosso
        })

    return events

# =========================
# Main Loop - SEMPLICE COME IL VECCHIO
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

            live = get_live_matches_with_odds()
            
            if not live:
                time.sleep(CHECK_INTERVAL)
                continue

            _loop += 1
            
            if _loop % 30 == 1:
                zero_zero = sum(1 for m in live if m["score"] == (0, 0))
                logger.info("📊 %d live | %d 0-0 | %d monitored | Stats: %d sent, %d won, %d lost", 
                           len(live), zero_zero, len(match_state),
                           daily_stats.total_sent, daily_stats.total_won, daily_stats.total_lost)
            
            # 🆕 Controllo reset giornaliero
            if daily_stats.check_if_need_reset():
                send_daily_report()
                daily_stats.reset()
                logger.info("🔄 Statistiche resettate per nuovo giorno")
            
            # 🆕 Controllo invio report ore 23:00
            current_hour = int(time.strftime("%H"))
            if (ENABLE_DAILY_STATS and current_hour == DAILY_REPORT_HOUR 
                and daily_stats.last_report_sent is None):
                if daily_stats.total_sent > 0:
                    send_daily_report()

            now = time.time()
            
            # 🆕 Aggiorna risultati segnali pending
            if ENABLE_DAILY_STATS:
                for signal in daily_stats.signals_sent:
                    if signal["status"] == "pending":
                        # Cerca il match corrispondente
                        for match in live:
                            if match["id"] == signal["id"]:
                                result = check_signal_result(
                                    signal, 
                                    match["score"], 
                                    match["ht_score"],
                                    match["minute"]
                                )
                                if result != "pending":
                                    daily_stats.update_signal(
                                        signal["id"], 
                                        match["score"], 
                                        result
                                    )
                                    status_emoji = "✅" if result == "won" else "❌"
                                    logger.info("%s Segnale %s: %s vs %s (%s) - Score: %d-%d", 
                                               status_emoji, result.upper(),
                                               signal["home"], signal["away"],
                                               signal["bet_type"],
                                               match["score"][0], match["score"][1])
                                
                                break

            for match in live:
                eid = match["id"]
                home = match["home"]
                away = match["away"]
                league = match["league"]
                cur_score = match["score"]
                ht_score = match["ht_score"]
                current_minute = match["minute"]
                home_price = match["home_price"]
                away_price = match["away_price"]
                has_red_card = match["has_red_card"]

                # Inizializza stato
                if eid not in match_state:
                    match_state[eid] = MatchState()
                    match_state[eid].first_seen_score = cur_score

                st = match_state[eid]
                
                # 🔥 STEP 0: Controlla cartellino rosso
                if has_red_card and not st.red_card_detected:
                    st.red_card_detected = True
                    logger.info("🟥 CARTELLINO ROSSO: %s vs %s - Match SCARTATO", home, away)
                    st.notified = True  # Blocca alert
                    continue
                
                if st.red_card_detected:
                    continue  # Skip match con rosso

                # STEP 1: Rileva goal CON CONFERMA (2 loop consecutivi)
                if st.goal_time is None:
                    first_score = st.first_seen_score or (0, 0)
                    
                    if first_score != (0, 0):
                        continue
                    
                    # Controlla se vediamo 1-0 o 0-1
                    if cur_score in [(1, 0), (0, 1)]:
                        # Se è lo stesso score della volta scorsa, aumenta counter
                        if st.pending_goal_score == cur_score:
                            st.pending_goal_count += 1
                            
                            # CONFERMA: visto 2 volte consecutive
                            if st.pending_goal_count >= 2:
                                st.goal_time = now
                                st.goal_minute = current_minute
                                st.scoring_team = "home" if cur_score == (1, 0) else "away"
                                logger.info("⚽ GOAL CONFERMATO %d': %s vs %s (%d-%d) | %s", 
                                           current_minute, home, away, 
                                           cur_score[0], cur_score[1], league)
                            else:
                                logger.info("⏳ Goal in attesa conferma (%d/2): %s vs %s (%d-%d)",
                                           st.pending_goal_count, home, away,
                                           cur_score[0], cur_score[1])
                        else:
                            # Primo rilevamento o score cambiato
                            st.pending_goal_score = cur_score
                            st.pending_goal_count = 1
                            logger.info("⏳ Goal rilevato (1/2): %s vs %s (%d-%d) - attendo conferma",
                                       home, away, cur_score[0], cur_score[1])
                        continue
                    else:
                        # Reset se torna 0-0 o altro
                        if st.pending_goal_score is not None:
                            logger.info("❌ Falso positivo annullato: %s vs %s (ora %d-%d)",
                                       home, away, cur_score[0], cur_score[1])
                            st.pending_goal_score = None
                            st.pending_goal_count = 0
                        continue

                # STEP 2: Verifica score non cambiato
                expected = (1, 0) if st.scoring_team == "home" else (0, 1)
                if cur_score != expected:
                    continue

                if st.notified:
                    continue

                # STEP 3: Attesa post-goal
                if now - st.goal_time < WAIT_AFTER_GOAL_SEC:
                    continue

                # STEP 4: Throttling per match
                if now - st.last_check < BASELINE_SAMPLE_INTERVAL:
                    continue

                st.tries += 1
                st.last_check = now

                # Prendi la quota giusta
                scorer_price = home_price if st.scoring_team == "home" else away_price
                
                if scorer_price is None:
                    st.consecutive_errors += 1
                    if st.consecutive_errors > 8:
                        logger.warning("⚠️ Skip %s vs %s (no odds)", home, away)
                        st.notified = True
                    continue

                st.consecutive_errors = 0

                # STEP 5: BASELINE
                if st.baseline is None:
                    if scorer_price < BASELINE_MIN or scorer_price > BASELINE_MAX:
                        logger.info("❌ %.2f fuori range [%.2f-%.2f]: %s vs %s", 
                                   scorer_price, BASELINE_MIN, BASELINE_MAX, home, away)
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

                # STEP 6: Monitora
                delta = scorer_price - st.baseline
                st.last_quote = scorer_price

                # Log variazioni
                if delta >= MIN_RISE * 0.7:
                    logger.info("📈 %d' | %s vs %s: %.2f (base %.2f, Δ+%.3f)", 
                               current_minute, home, away, scorer_price, st.baseline, delta)

                # STEP 7: Alert - DUE STRATEGIE
                # 🔥 La finestra si basa sul MINUTO ATTUALE (cambio quote), non sul minuto del goal
                
                # Controlla che quota finale non superi 2.00
                if scorer_price > MAX_FINAL_QUOTE:
                    logger.warning("⚠️ Quota finale %.2f > %.2f: %s vs %s - SCARTATO", 
                                  scorer_price, MAX_FINAL_QUOTE, home, away)
                    st.notified = True
                    continue
                
                # 🔥 Determina strategia in base al MINUTO ATTUALE (quando quote cambia)
                if GOAL_WINDOW_1_MIN <= current_minute <= GOAL_WINDOW_1_MAX:
                    # Finestra 1: 25-60' → OVER 2.5 FINALE
                    bet_type = "OVER 2.5 FINALE"
                    
                    # Controlla quota minima 1.50 per OVER 2.5
                    if scorer_price < MIN_OVER25_QUOTE:
                        logger.warning("⚠️ Quota %.2f < %.2f (troppo bassa per OVER 2.5): %s vs %s - SCARTATO", 
                                      scorer_price, MIN_OVER25_QUOTE, home, away)
                        st.notified = True
                        continue
                
                elif GOAL_WINDOW_2_MIN <= current_minute <= GOAL_WINDOW_2_MAX:
                    # Finestra 2: 60-80' → OVER 1.5 FINALE
                    bet_type = "OVER 1.5 FINALE"
                    # Nessun controllo quota minima per OVER 1.5
                
                else:
                    # Fuori da entrambe le finestre
                    logger.info("⏭️ Cambio quote al %d' fuori finestre [%d-%d] e [%d-%d]: %s vs %s - SKIP",
                               current_minute,
                               GOAL_WINDOW_1_MIN, GOAL_WINDOW_1_MAX,
                               GOAL_WINDOW_2_MIN, GOAL_WINDOW_2_MAX,
                               home, away)
                    st.notified = True
                    continue
                
                if delta >= MIN_RISE:
                    team_name = home if st.scoring_team == "home" else away
                    team_label = "1" if st.scoring_team == "home" else "2"
                    pct = (delta / st.baseline * 100)
                    
                    msg = (
                        f"💰💎 <b>QUOTE JUMP</b> 💎💰\n\n"
                        f"🏆 {league}\n"
                        f"⚽ <b>{home}</b> vs <b>{away}</b>\n"
                        f"📊 <b>{cur_score[0]}-{cur_score[1]}</b> ({current_minute}')\n\n"
                        f"⚽ Goal al {st.goal_minute}'\n"
                        f"💸 Quota <b>{team_label}</b> ({team_name}):\n"
                        f"<b>{st.baseline:.2f}</b> → <b>{scorer_price:.2f}</b>\n"
                        f"📈 <b>+{delta:.2f}</b> (+{pct:.1f}%)\n\n"
                        f"🎯 <b>GIOCA: {bet_type}</b> 🎯\n"
                        f"💰 <b>Stake: €{STAKE}</b>"
                    )
                    
                    if send_telegram_message(msg):
                        logger.info("✅ ALERT %d': %s vs %s | Goal %d' | %.2f→%.2f (+%.2f) | %s | €%d", 
                                   current_minute, home, away, st.goal_minute, st.baseline, scorer_price, delta, bet_type, STAKE)
                        
                        # Traccia segnale nelle statistiche
                        if ENABLE_DAILY_STATS:
                            daily_stats.add_signal(
                                match_id=eid,
                                home=home,
                                away=away,
                                league=league,
                                bet_type=bet_type,
                                goal_minute=st.goal_minute,
                                baseline=st.baseline,
                                final_quote=scorer_price,
                                delta=delta,
                                timestamp=now
                            )
                    
                    st.notified = True
                
                elif delta > MAX_RISE:
                    logger.warning("⚠️ Delta troppo alto (+%.2f > %.2f): %s vs %s - SKIP", 
                                  delta, MAX_RISE, home, away)
                    st.notified = True

            # Pulizia
            to_remove = []
            for k, v in match_state.items():
                age = now - v.first_seen_at
                if age > 7200:
                    to_remove.append(k)
            
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
    logger.info("🚀 BOT DUAL STRATEGY v6.0")
    logger.info("="*60)
    logger.info("⚙️  Config:")
    logger.info("   • Window 1: %d'-%d' → OVER 2.5", GOAL_WINDOW_1_MIN, GOAL_WINDOW_1_MAX)
    logger.info("   • Window 2: %d'-%d' → OVER 1.5", GOAL_WINDOW_2_MIN, GOAL_WINDOW_2_MAX)
    logger.info("   • Quote: %.2f-%.2f | Over 2.5 min: %.2f", BASELINE_MIN, BASELINE_MAX, MIN_OVER25_QUOTE)
    logger.info("   • Rise: +%.2f | Max: %.2f", MIN_RISE, MAX_FINAL_QUOTE)
    logger.info("   • Stake: €%d", STAKE)
    logger.info("   • Protections: Red card, 2-loop confirm")
    logger.info("   • Report: %02d:00", DAILY_REPORT_HOUR)
    logger.info("="*60)
    
    send_telegram_message(
        f"🤖 <b>Bot DUAL STRATEGY</b> v6.0 ⚡\n\n"
        f"⚽ <b>DUE FINESTRE:</b>\n"
        f"🎯 {GOAL_WINDOW_1_MIN}'-{GOAL_WINDOW_1_MAX}' → <b>OVER 2.5</b> (€{STAKE})\n"
        f"🎯 {GOAL_WINDOW_2_MIN}'-{GOAL_WINDOW_2_MAX}' → <b>OVER 1.5</b> (€{STAKE})\n\n"
        f"📊 <b>Parametri:</b>\n"
        f"• Quote: {BASELINE_MIN:.2f} - {BASELINE_MAX:.2f}\n"
        f"• Rise: +{MIN_RISE:.2f} | Max: {MAX_FINAL_QUOTE:.2f}\n"
        f"• Over 2.5 min: {MIN_OVER25_QUOTE:.2f}\n\n"
        f"🛡️ <b>Protezioni:</b>\n"
        f"• Conferma goal (2-loop)\n"
        f"• Auto-scarta con rosso 🟥\n\n"
        f"📊 Report: 00:00\n\n"
        f"🔍 Monitoraggio attivo!"
    )
    
    main_loop()

if __name__ == "__main__":
    main()
