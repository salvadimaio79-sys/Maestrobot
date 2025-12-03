import os
import sys
import time
import logging
import requests
import unicodedata
import re
from datetime import datetime, timezone
from collections import deque, defaultdict

# =========================
# CONFIGURAZIONE
# =========================
RAPIDAPI_KEY  = os.getenv("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = os.getenv("RAPIDAPI_HOST", "soccer-football-info.p.rapidapi.com")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")  # Render usa "CHAT_ID" non "TELEGRAM_CHAT_ID"

# Debug
if TELEGRAM_TOKEN and CHAT_ID:
    print(f"✅ Telegram configurato: {TELEGRAM_TOKEN[:10]}... → {CHAT_ID}")
else:
    print(f"❌ Telegram mancante: TOKEN={bool(TELEGRAM_TOKEN)} CHAT={bool(CHAT_ID)}")

# Business rules
MIN_RISE        = float(os.getenv("MIN_RISE", "0.06"))
MAX_RISE        = float(os.getenv("MAX_RISE", "0.70"))
BASELINE_MIN    = float(os.getenv("BASELINE_MIN", "1.30"))
BASELINE_MAX    = float(os.getenv("BASELINE_MAX", "1.75"))
MAX_FINAL_QUOTE = float(os.getenv("MAX_FINAL_QUOTE", "2.00"))
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL_SECONDS", "4"))
WAIT_AFTER_GOAL_SEC = int(os.getenv("WAIT_AFTER_GOAL_SEC", "10"))

# Baseline sampling
BASELINE_SAMPLES = int(os.getenv("BASELINE_SAMPLES", "2"))
BASELINE_SAMPLE_INTERVAL = int(os.getenv("BASELINE_SAMPLE_INTERVAL", "4"))

# Strategia HT Recovery
GOAL_MINUTE_MAX_HT = int(os.getenv("GOAL_MINUTE_MAX_HT", "25"))

# Stake
STAKE = int(os.getenv("STAKE", "50"))

# Rate limiting
MAX_API_RETRIES = 2
API_RETRY_DELAY = 1
COOLDOWN_ON_DAILY_429_MIN = int(os.getenv("COOLDOWN_ON_DAILY_429_MIN", "30"))
_last_daily_429_ts = 0

# Report giornaliero
ENABLE_DAILY_STATS = os.getenv("ENABLE_DAILY_STATS", "true").lower() == "true"
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "0"))

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# =========================
# FILTRI LEGHE
# =========================
LEAGUE_EXCLUDE_KEYWORDS = [
    "esoccer", "e-soccer", "cyber", "e-football", 
    "esports", "fifa", "pes", "efootball",
    "virtual", "simulated", "gtworld", "baller",
    "6 mins", "8 mins", "10 mins", "12 mins", 
    "15 mins", "20 mins", "30 mins", "h2h gg",
    "women", "woman", "w)", "(w", "feminine", "femminile", "donne",
    "indonesia", "indonesian",
]

SPAIN_ALLOWED_LEAGUES = [
    "la liga", "laliga", "primera division", "primera división"
]

def is_excluded_league(league_name: str) -> bool:
    league_lower = league_name.lower()
    
    for keyword in LEAGUE_EXCLUDE_KEYWORDS:
        if keyword.lower() in league_lower:
            return True
    
    if "spain" in league_lower or "spagna" in league_lower or "spanish" in league_lower or "españa" in league_lower:
        for allowed in SPAIN_ALLOWED_LEAGUES:
            if allowed.lower() in league_lower:
                return False
        return True
    
    return False

HEADERS = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPIDAPI_HOST}

# =========================
# STATO MATCH
# =========================
class MatchState:
    __slots__ = ("first_seen_at", "first_seen_score", "goal_time", "goal_minute",
                 "scoring_team", "baseline_samples", "baseline", "last_quote", 
                 "notified", "tries", "last_check", "consecutive_errors",
                 "pending_goal_score", "pending_goal_count", "pre_goal_quote",
                 "red_card_detected", "sent_ht_alert", "ht_result", "sent_ft_recovery")
    
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
        self.red_card_detected = False
        self.sent_ht_alert = False
        self.ht_result = None
        self.sent_ft_recovery = False

match_state = {}
_loop = 0

# =========================
# STATISTICHE GIORNALIERE
# =========================
class DailyStats:
    def __init__(self):
        self.reset_date = datetime.now().strftime("%Y-%m-%d")
        self.signals_sent = []
        self.total_sent = 0
        self.total_won = 0
        self.total_lost = 0
        self.total_pending = 0
        self.last_report_sent = 0
        
    def add_signal(self, match_id, home, away, league, bet_type, goal_minute, baseline, final_quote, delta, timestamp):
        signal = {
            "match_id": match_id,
            "home": home,
            "away": away,
            "league": league,
            "bet_type": bet_type,
            "goal_minute": goal_minute,
            "baseline": baseline,
            "final_quote": final_quote,
            "delta": delta,
            "timestamp": timestamp,
            "status": "pending",
            "final_score": None
        }
        self.signals_sent.append(signal)
        self.total_sent += 1
        self.total_pending += 1
        
    def reset(self):
        self.__init__()

daily_stats = DailyStats()

# =========================
# UTILITY FUNCTIONS
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
    if signal["status"] != "pending":
        return signal["status"]
    
    bet_type = signal.get("bet_type", "")
    
    if "HT" in bet_type or "PRIMO TEMPO" in bet_type:
        if current_minute >= 45:
            if ht_score is not None:
                ht_total = ht_score[0] + ht_score[1]
                return "won" if ht_total >= 2 else "lost"
            elif current_minute > 45:
                return "lost"
        return "pending"
    
    elif "FT" in bet_type or "FINALE" in bet_type:
        if current_minute < 90:
            return "pending"
        
        final_total = current_score[0] + current_score[1]
        return "won" if final_total >= 3 else "lost"
    
    return "pending"

def send_daily_report():
    if not ENABLE_DAILY_STATS:
        return
    
    if daily_stats.total_sent == 0:
        msg = (
            f"📊 <b>REPORT GIORNALIERO</b> 📊\n"
            f"📅 {daily_stats.reset_date}\n\n"
            f"📭 Nessun segnale oggi\n\n"
            f"🔄 Statistiche resettate"
        )
        send_telegram_message(msg)
        logger.info("📊 Report: 0 segnali")
        daily_stats.last_report_sent = time.time()
        return
    
    ht_signals = [s for s in daily_stats.signals_sent if "HT" in s["bet_type"]]
    ft_signals = [s for s in daily_stats.signals_sent if "FT" in s["bet_type"]]
    
    ht_won = len([s for s in ht_signals if s["status"] == "won"])
    ht_lost = len([s for s in ht_signals if s["status"] == "lost"])
    ht_pending = len([s for s in ht_signals if s["status"] == "pending"])
    
    ft_won = len([s for s in ft_signals if s["status"] == "won"])
    ft_lost = len([s for s in ft_signals if s["status"] == "lost"])
    ft_pending = len([s for s in ft_signals if s["status"] == "pending"])
    
    ht_wr = (ht_won / len(ht_signals) * 100) if ht_signals else 0
    ft_wr = (ft_won / len(ft_signals) * 100) if ft_signals else 0
    total_wr = (daily_stats.total_won / daily_stats.total_sent * 100) if daily_stats.total_sent > 0 else 0
    
    msg = (
        f"📊 <b>REPORT GIORNALIERO</b> 📊\n"
        f"📅 {daily_stats.reset_date}\n\n"
        f"📨 Totale: <b>{daily_stats.total_sent}</b>\n"
        f"✅ Vinti: <b>{daily_stats.total_won}</b>\n"
        f"❌ Persi: <b>{daily_stats.total_lost}</b>\n"
        f"⏳ Pending: <b>{daily_stats.total_pending}</b>\n"
        f"📈 Win Rate: <b>{total_wr:.1f}%</b>\n\n"
    )
    
    if ht_signals:
        msg += (
            f"⏰ <b>OVER 1.5 HT ({len(ht_signals)})</b>\n"
            f"✅ {ht_won} | ❌ {ht_lost} | ⏳ {ht_pending}\n"
            f"📈 WR: {ht_wr:.1f}%\n\n"
        )
        
        won_ht = [s for s in ht_signals if s["status"] == "won"][:5]
        if won_ht:
            msg += "✅ <b>Vincenti HT:</b>\n"
            for i, s in enumerate(won_ht, 1):
                msg += f"{i}. {s['home']} vs {s['away']}\n"
            msg += "\n"
        
        lost_ht = [s for s in ht_signals if s["status"] == "lost"][:5]
        if lost_ht:
            msg += "❌ <b>Perdenti HT:</b>\n"
            for i, s in enumerate(lost_ht, 1):
                msg += f"{i}. {s['home']} vs {s['away']}\n"
            msg += "\n"
    
    if ft_signals:
        msg += (
            f"🎯 <b>OVER 2.5 FT ({len(ft_signals)})</b>\n"
            f"✅ {ft_won} | ❌ {ft_lost} | ⏳ {ft_pending}\n"
            f"📈 WR: {ft_wr:.1f}%\n\n"
        )
        
        won_ft = [s for s in ft_signals if s["status"] == "won"][:5]
        if won_ft:
            msg += "✅ <b>Vincenti FT:</b>\n"
            for i, s in enumerate(won_ft, 1):
                msg += f"{i}. {s['home']} vs {s['away']}\n"
            msg += "\n"
        
        lost_ft = [s for s in ft_signals if s["status"] == "lost"][:5]
        if lost_ft:
            msg += "❌ <b>Perdenti FT:</b>\n"
            for i, s in enumerate(lost_ft, 1):
                msg += f"{i}. {s['home']} vs {s['away']}\n"
            msg += "\n"
    
    msg += "🔄 Statistiche resettate"
    
    send_telegram_message(msg)
    logger.info("📊 Report inviato")
    daily_stats.last_report_sent = time.time()

def get_live_events():
    url = f"https://{RAPIDAPI_HOST}/api/liveevents"
    r = http_get(url, headers=HEADERS, timeout=12)
    if not r:
        return []
    
    try:
        data = r.json()
    except:
        return []
    
    if not isinstance(data, dict):
        return []
    
    results = data.get("result", [])
    if not isinstance(results, list):
        return []
    
    events = []
    seen_signatures = set()
    
    for match in results:
        if not isinstance(match, dict):
            continue
        
        event_id = match.get("id")
        if not event_id:
            continue
        
        league = match.get("league", "")
        home = match.get("homeTeam", "")
        away = match.get("awayTeam", "")
        
        if not all([league, home, away]):
            continue
        
        if is_excluded_league(league):
            continue
        
        status_str = str(match.get("status", "")).lower()
        if "in play" not in status_str:
            continue
        
        timer = match.get("timer", {})
        if not isinstance(timer, dict):
            continue
        
        current_minute = timer.get("tm", 0)
        if not isinstance(current_minute, int):
            try:
                current_minute = int(current_minute)
            except:
                current_minute = 0
        
        score_a = match.get("scores", {}).get("score", {})
        if not isinstance(score_a, dict):
            continue
        
        h_score = score_a.get("home")
        a_score = score_a.get("away")
        
        if h_score is None or a_score is None:
            continue
        
        try:
            h_score = int(h_score)
            a_score = int(a_score)
        except:
            continue
        
        cur_score = (h_score, a_score)
        
        ht_a = match.get("scores", {}).get("ht", {})
        ht_score = None
        if isinstance(ht_a, dict):
            ht_h = ht_a.get("home")
            ht_away = ht_a.get("away")
            if ht_h is not None and ht_away is not None:
                try:
                    ht_score = (int(ht_h), int(ht_away))
                except:
                    pass
        
        rc_a = score_a.get("rc", 0)
        rc_b = score_a.get("rc", 0)
        has_red_card = False
        try:
            if int(rc_a) > 0 or int(rc_b) > 0:
                has_red_card = True
        except:
            pass
        
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
            "has_red_card": has_red_card
        })
    
    return events

def main_loop():
    global _loop, match_state
    
    now = time.time()
    _loop += 1
    
    if ENABLE_DAILY_STATS:
        current_hour = datetime.now().hour
        if current_hour == DAILY_REPORT_HOUR:
            if (now - daily_stats.last_report_sent) > 3600:
                send_daily_report()
                daily_stats.reset()
    
    if _last_daily_429_ts > 0:
        elapsed = (now - _last_daily_429_ts) / 60
        if elapsed < COOLDOWN_ON_DAILY_429_MIN:
            return
    
    events = get_live_events()
    
    if ENABLE_DAILY_STATS:
        for signal in daily_stats.signals_sent:
            if signal["status"] != "pending":
                continue
            
            match_id = signal["match_id"]
            matching_event = next((e for e in events if e["id"] == match_id), None)
            
            if matching_event:
                old_status = signal["status"]
                new_status = check_signal_result(
                    signal,
                    matching_event["score"],
                    matching_event["ht_score"],
                    matching_event["minute"]
                )
                
                if new_status != old_status:
                    signal["status"] = new_status
                    signal["final_score"] = matching_event["score"]
                    
                    if new_status == "won":
                        daily_stats.total_won += 1
                        daily_stats.total_pending -= 1
                    elif new_status == "lost":
                        daily_stats.total_lost += 1
                        daily_stats.total_pending -= 1
    
    monitored_count = 0
    zero_zero_count = 0
    
    for evt in events:
        eid = evt["id"]
        home = evt["home"]
        away = evt["away"]
        league = evt["league"]
        cur_score = evt["score"]
        ht_score = evt["ht_score"]
        current_minute = evt["minute"]
        home_price = evt["home_price"]
        away_price = evt["away_price"]
        has_red_card = evt["has_red_card"]
        
        if cur_score == (0, 0):
            zero_zero_count += 1
        
        if eid not in match_state:
            match_state[eid] = MatchState()
            match_state[eid].first_seen_score = cur_score
        
        st = match_state[eid]
        
        if has_red_card:
            if not st.red_card_detected:
                st.red_card_detected = True
                logger.info("🟥 ROSSO: %s vs %s", home, away)
                st.notified = True
            continue
        
        if st.red_card_detected:
            continue
        
        # Check HT result for recovery
        if st.sent_ht_alert and not st.sent_ft_recovery:
            if current_minute >= 45 and ht_score is not None:
                ht_total = ht_score[0] + ht_score[1]
                
                if ht_total < 2:
                    st.ht_result = "lost"
                    st.sent_ft_recovery = True
                    
                    if st.scoring_team and home_price and away_price:
                        scorer_price = home_price if st.scoring_team == "home" else away_price
                        
                        if BASELINE_MIN <= scorer_price <= MAX_FINAL_QUOTE:
                            team_name = home if st.scoring_team == "home" else away
                            team_label = "1" if st.scoring_team == "home" else "2"
                            
                            msg = (
                                f"💰💎 <b>QUOTE JUMP</b> 💎💰\n\n"
                                f"🏆 {league}\n"
                                f"⚽ <b>{home}</b> vs <b>{away}</b>\n"
                                f"📊 <b>{cur_score[0]}-{cur_score[1]}</b> ({current_minute}')\n\n"
                                f"⚽ Goal al {st.goal_minute}'\n"
                                f"💸 Quota <b>{team_label}</b> ({team_name}): <b>{scorer_price:.2f}</b>\n\n"
                                f"🎯 <b>GIOCA: OVER 2.5 FT</b> 🎯\n"
                                f"💰 <b>Stake: €{STAKE}</b>"
                            )
                            
                            if send_telegram_message(msg):
                                logger.info("✅ RECOVERY %d': %s vs %s | OVER 2.5 FT", 
                                           current_minute, home, away)
                                
                                if ENABLE_DAILY_STATS:
                                    daily_stats.add_signal(
                                        match_id=eid,
                                        home=home,
                                        away=away,
                                        league=league,
                                        bet_type="OVER 2.5 FT",
                                        goal_minute=st.goal_minute,
                                        baseline=scorer_price,
                                        final_quote=scorer_price,
                                        delta=0,
                                        timestamp=now
                                    )
                else:
                    st.ht_result = "won"
        
        if st.notified:
            continue
        
        monitored_count += 1
        
        # Detect goal
        if st.goal_time is None:
            if cur_score in [(1, 0), (0, 1)] and st.first_seen_score == (0, 0):
                if st.pending_goal_score == cur_score:
                    st.pending_goal_count += 1
                    
                    if st.pending_goal_count >= 2:
                        st.goal_time = now
                        st.goal_minute = current_minute
                        st.scoring_team = "home" if cur_score == (1, 0) else "away"
                        logger.info("⚽ GOAL %d': %s vs %s (%d-%d)", 
                                   current_minute, home, away, cur_score[0], cur_score[1])
                else:
                    st.pending_goal_score = cur_score
                    st.pending_goal_count = 1
            
            continue
        
        if (now - st.goal_time) < WAIT_AFTER_GOAL_SEC:
            continue
        
        if home_price is None and away_price is None:
            st.consecutive_errors += 1
            if st.consecutive_errors > 8:
                st.notified = True
            continue
        
        scorer_price = home_price if st.scoring_team == "home" else away_price
        
        if scorer_price is None:
            st.consecutive_errors += 1
            if st.consecutive_errors > 8:
                st.notified = True
            continue
        
        st.consecutive_errors = 0
        
        # Baseline
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
            
            st.last_quote = scorer_price
            continue
        
        # Monitor
        delta = scorer_price - st.baseline
        st.last_quote = scorer_price
        
        # Alert OVER 1.5 HT
        if scorer_price > MAX_FINAL_QUOTE:
            st.notified = True
            continue
        
        if current_minute > GOAL_MINUTE_MAX_HT:
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
                f"⏰ <b>GIOCA: OVER 1.5 HT</b> ⏰\n"
                f"💰 <b>Stake: €{STAKE}</b>"
            )
            
            if send_telegram_message(msg):
                logger.info("✅ ALERT HT %d': %s vs %s | %.2f→%.2f", 
                           current_minute, home, away, st.baseline, scorer_price)
                
                st.sent_ht_alert = True
                st.notified = True
                
                if ENABLE_DAILY_STATS:
                    daily_stats.add_signal(
                        match_id=eid,
                        home=home,
                        away=away,
                        league=league,
                        bet_type="OVER 1.5 HT",
                        goal_minute=st.goal_minute,
                        baseline=st.baseline,
                        final_quote=scorer_price,
                        delta=delta,
                        timestamp=now
                    )
    
    if _loop % 15 == 0:
        logger.info("📊 %d live | %d 0-0 | %d monitored | Stats: %d sent, %d won, %d lost", 
                   len(events), zero_zero_count, monitored_count,
                   daily_stats.total_sent, daily_stats.total_won, daily_stats.total_lost)

def main():
    logger.info("="*60)
    logger.info("🚀 BOT HT RECOVERY v8.3")
    logger.info("="*60)
    logger.info("⚙️  Config:")
    logger.info("   • OVER 1.5 HT (quote ≤%d')", GOAL_MINUTE_MAX_HT)
    logger.info("   • HT perso → OVER 2.5 FT")
    logger.info("   • Quote: %.2f-%.2f | Max: %.2f", BASELINE_MIN, BASELINE_MAX, MAX_FINAL_QUOTE)
    logger.info("   • Rise: +%.2f | Stake: €%d", MIN_RISE, STAKE)
    logger.info("="*60)
    
    send_telegram_message(
        f"🤖 <b>Bot HT RECOVERY</b> v8.3 ⚡\n\n"
        f"⚽ <b>STRATEGIA:</b>\n"
        f"1️⃣ Quote ≤{GOAL_MINUTE_MAX_HT}' → <b>OVER 1.5 HT</b>\n"
        f"2️⃣ HT perso → <b>OVER 2.5 FT</b>\n\n"
        f"📊 Quote: {BASELINE_MIN:.2f}-{BASELINE_MAX:.2f}\n"
        f"💰 Stake: €{STAKE}\n\n"
        f"🔍 Attivo!"
    )
    
    while True:
        try:
            main_loop()
        except KeyboardInterrupt:
            logger.info("⛔ Stop")
            break
        except Exception as e:
            logger.error("❌ Error: %s", e, exc_info=True)
        
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
