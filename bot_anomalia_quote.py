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
logger = logging.getLogger("ht-recovery-bot")

# =========================
# Environment
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = os.getenv("RAPIDAPI_HOST", "soccer-football-info.p.rapidapi.com")
RAPIDAPI_BASE = f"https://{RAPIDAPI_HOST}"

RAPIDAPI_LIVE_FULL_PATH = "/live/full/"
RAPIDAPI_LIVE_PARAMS = {"i": "en_US", "f": "json", "e": "no"}

# Business rules
MIN_RISE = float(os.getenv("MIN_RISE", "0.06"))
BASELINE_MIN = float(os.getenv("BASELINE_MIN", "1.30"))
BASELINE_MAX = float(os.getenv("BASELINE_MAX", "1.75"))
MAX_FINAL_QUOTE = float(os.getenv("MAX_FINAL_QUOTE", "2.00"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "4"))
WAIT_AFTER_GOAL_SEC = int(os.getenv("WAIT_AFTER_GOAL_SEC", "10"))

BASELINE_SAMPLES = int(os.getenv("BASELINE_SAMPLES", "2"))
BASELINE_SAMPLE_INTERVAL = int(os.getenv("BASELINE_SAMPLE_INTERVAL", "6"))

# HT
GOAL_MINUTE_MAX_HT = int(os.getenv("GOAL_MINUTE_MAX_HT", "25"))
STAKE_HT = int(os.getenv("STAKE_HT", "20"))

# Rate limiting
API_CALL_MIN_GAP_MS = int(os.getenv("API_CALL_MIN_GAP_MS", "300"))
_last_api_call_ts_ms = 0

RECENT_GOAL_PRIORITY_SEC = 120

COOLDOWN_ON_DAILY_429_MIN = int(os.getenv("COOLDOWN_ON_DAILY_429_MIN", "30"))
_last_daily_429_ts = 0

MAX_API_RETRIES = 2
API_RETRY_DELAY = 1

# FILTRI - Solo eSports/Virtual
LEAGUE_EXCLUDE_KEYWORDS = [
    "esoccer", "8 mins", "volta", "h2h gg", "virtual", 
    "baller", "30 mins", "20 mins", "10 mins", "12 mins",
    "cyber", "e-football", "esports", "fifa", "pes",
    "simulated", "gtworld", "6 mins", "15 mins",
]

HEADERS = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPIDAPI_HOST}

# =========================
# Stato match
# =========================
class MatchState:
    __slots__ = ("first_seen_at", "first_seen_score", "goal_time", "goal_minute",
                 "scoring_team", "baseline_samples", "baseline", "last_quote", 
                 "notified", "tries", "last_check", "consecutive_errors", "last_seen_loop")
    
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
        self.last_seen_loop = 0

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
            logger.warning("Telegram error: %s", e)
        
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
                    logger.error("❌ DAILY QUOTA")
                    return None
                if attempt < retries - 1:
                    time.sleep(API_RETRY_DELAY)
                    continue
            
            if r.ok:
                return r
            
            if attempt < retries - 1:
                time.sleep(API_RETRY_DELAY)
                
        except:
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

def is_excluded_league(league_name: str) -> bool:
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
    url = f"{RAPIDAPI_BASE.rstrip('/')}/{RAPIDAPI_LIVE_FULL_PATH.lstrip('/')}"
    r = http_get(url, headers=HEADERS, params=RAPIDAPI_LIVE_PARAMS, timeout=20)
    
    if not r or not r.ok:
        return []

    try:
        data = r.json() or {}
    except:
        return []

    raw_events = data.get("result") or []
    events = []
    seen_signatures = set()

    for match in raw_events:
        event_id = str(match.get("id", ""))
        timer = match.get("timer", "")
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
        cur_score = parse_score_tuple(score_home, score_away)
        
        # HT score
        ht_home = score_a.get("ht", "0")
        ht_away = score_b.get("ht", "0")
        ht_score = parse_score_tuple(ht_home, ht_away)
        
        current_minute = parse_timer_to_minutes(timer)

        odds_data = match.get("odds") or {}
        live_odds = odds_data.get("live") or {}
        odds_1x2 = live_odds.get("1X2") or {}
        bet365_odds = odds_1x2.get("bet365") or {}
        
        home_price = parse_price(bet365_odds.get("1"))
        draw_price = parse_price(bet365_odds.get("X"))
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
            "odds": {
                "home": home_price,
                "draw": draw_price,
                "away": away_price
            }
        })

    return events

# =========================
# Main Loop
# =========================
def main_loop():
    global _last_daily_429_ts, _loop

    while True:
        try:
            if _last_daily_429_ts:
                elapsed = int(time.time()) - _last_daily_429_ts
                if elapsed < COOLDOWN_ON_DAILY_429_MIN * 60:
                    time.sleep(CHECK_INTERVAL)
                    continue
                _last_daily_429_ts = 0

            if not can_call_api():
                time.sleep(0.5)
                continue

            live = get_live_matches_with_odds()
            mark_api_call()
            
            if not live:
                time.sleep(CHECK_INTERVAL)
                continue

            _loop += 1
            
            current_match_ids = {m["id"] for m in live}
            for eid in current_match_ids:
                if eid in match_state:
                    match_state[eid].last_seen_loop = _loop

            if _loop % 30 == 1:
                logger.info("📊 %d live | %d monitored", len(live), len(match_state))

            now = time.time()

            # Priority
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
            
            all_matches = prioritized_matches + other_matches

            for match in all_matches:
                eid = match["id"]
                home = match["home"]
                away = match["away"]
                league = match["league"]
                cur_score = match["score"]
                ht_score = match["ht_score"]
                current_minute = match["minute"]
                odds = match["odds"]

                if eid not in match_state:
                    match_state[eid] = MatchState()
                    match_state[eid].first_seen_score = cur_score
                    match_state[eid].last_seen_loop = _loop

                st = match_state[eid]
                st.last_seen_loop = _loop

                # ============================================
                # GOAL DETECTION
                # ============================================
                if st.goal_time is None:
                    first_score = st.first_seen_score or (0, 0)
                    
                    if first_score != (0, 0):
                        continue
                    
                    if cur_score == (1, 0):
                        st.goal_time = now
                        st.goal_minute = current_minute
                        st.scoring_team = "home"
                        logger.info("⚽ GOAL %d': %s vs %s (1-0) | %s - ATTESA CONFERMA VAR", 
                                   current_minute, home, away, league)
                        continue
                    elif cur_score == (0, 1):
                        st.goal_time = now
                        st.goal_minute = current_minute
                        st.scoring_team = "away"
                        logger.info("⚽ GOAL %d': %s vs %s (0-1) | %s - ATTESA CONFERMA VAR", 
                                   current_minute, home, away, league)
                        continue
                    else:
                        continue

                # VERIFICA GOAL ANCORA VALIDO (dopo wait post-goal)
                expected = (1, 0) if st.scoring_team == "home" else (0, 1)
                if cur_score != expected:
                    # Goal annullato o cambiato score!
                    if st.goal_time and (now - st.goal_time) > WAIT_AFTER_GOAL_SEC:
                        logger.warning("🚫 GOAL ANNULLATO: %s vs %s (era %s, ora %d-%d)", 
                                      home, away, expected, cur_score[0], cur_score[1])
                        st.notified = True  # Skip questo match
                    continue

                if st.notified:
                    continue

                # Wait post-goal (CONFERMA VAR)
                if now - st.goal_time < WAIT_AFTER_GOAL_SEC:
                    # Ancora in attesa conferma
                    continue
                
                # Goal confermato! Log solo la prima volta
                if st.tries == 0:
                    logger.info("✅ GOAL CONFERMATO %d': %s vs %s (%d-%d)", 
                               current_minute, home, away, cur_score[0], cur_score[1])

                # Throttling
                if now - st.last_check < BASELINE_SAMPLE_INTERVAL:
                    continue

                st.tries += 1
                st.last_check = now

                scorer_price = odds["home"] if st.scoring_team == "home" else odds["away"]
                
                if scorer_price is None:
                    st.consecutive_errors += 1
                    if st.consecutive_errors > 8:
                        st.notified = True
                    continue

                st.consecutive_errors = 0

                # BASELINE
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

                # Monitor
                delta = scorer_price - st.baseline
                st.last_quote = scorer_price

                if delta >= MIN_RISE * 0.7:
                    logger.info("📈 %d' | %s vs %s: %.2f (base %.2f, Δ+%.3f)", 
                               current_minute, home, away, scorer_price, st.baseline, delta)

                # MAX QUOTA
                if scorer_price > MAX_FINAL_QUOTE:
                    logger.info("⚠️ Quota %.2f > %.2f: SKIP", scorer_price, MAX_FINAL_QUOTE)
                    st.notified = True
                    continue

                # ============================================
                # ALERT OVER 1.5 HT
                # ============================================
                if delta >= MIN_RISE:
                    # CONTROLLO CRUCIALE: Il cambio quote deve avvenire entro 25'!
                    if current_minute > GOAL_MINUTE_MAX_HT:
                        logger.info("⏭️ Varianza quote al %d' (oltre %d'): %s vs %s - SKIP", 
                                   current_minute, GOAL_MINUTE_MAX_HT, home, away)
                        st.notified = True
                        continue
                    
                    # ⚠️ FIX DUPLICATI: Setto notified = True PRIMA dell'invio
                    st.notified = True
                    
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
                        f"<b><u>⏰ GIOCA: OVER 1.5 PRIMO TEMPO ⏰</u></b>\n"
                        f"💰 <b>Stake: €{STAKE_HT}</b>"
                    )
                    
                    if send_telegram_message(msg):
                        logger.info("✅ ALERT HT %d': %s vs %s | %.2f→%.2f (+%.2f)", 
                                   current_minute, home, away, st.baseline, scorer_price, delta)
                        st.sent_ht_alert = True

            # Cleanup
            to_remove = [k for k, v in match_state.items() 
                        if (_loop - v.last_seen_loop) > 2 or (now - v.first_seen_at) > 7200]
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
    logger.info("🚀 BOT OVER 1.5 HT FINALE")
    logger.info("="*60)
    logger.info("   ⚽ Goal + VARIANZA QUOTE entro %d' → OVER 1.5 PRIMO TEMPO (€%d)", GOAL_MINUTE_MAX_HT, STAKE_HT)
    logger.info("   📊 Quote: %.2f-%.2f | Max: %.2f", BASELINE_MIN, BASELINE_MAX, MAX_FINAL_QUOTE)
    logger.info("   📈 Rise: +%.2f", MIN_RISE)
    logger.info("   🛡️ Protezione VAR: %ds", WAIT_AFTER_GOAL_SEC)
    logger.info("="*60)
    
    send_telegram_message(
        f"🤖 <b>Bot OVER 1.5 HT</b> FINALE ⚡\n\n"
        f"⚽ Goal + Quote ↑ <b>entro {GOAL_MINUTE_MAX_HT}'</b>\n"
        f"   → <b>OVER 1.5 PRIMO TEMPO</b> (€{STAKE_HT})\n\n"
        f"📊 Quote: {BASELINE_MIN:.2f}-{BASELINE_MAX:.2f}\n"
        f"📈 Rise: +{MIN_RISE:.2f} | Max: {MAX_FINAL_QUOTE:.2f}\n"
        f"🛡️ Protezione VAR: {WAIT_AFTER_GOAL_SEC}s\n\n"
        f"⚠️ Varianza quote DEVE avvenire entro {GOAL_MINUTE_MAX_HT}'\n\n"
        f"🔍 Attivo!"
    )
    
    main_loop()

if __name__ == "__main__":
    main()
