#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot Anomalia Quote - v8.0 FINAL
Strategia HT Recovery:
- Goal entro 25' (al momento cambio quote) → OVER 1.5 HT
- Se HT perso → OVER 2.5 FT (stesso match, nuovo segnale)
"""

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
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

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

# 🔥 STRATEGIA HT RECOVERY:
# Cambio quote entro 25' → OVER 1.5 HT
# Se HT perso → OVER 2.5 FT
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
    # eSports/Virtual
    "esoccer", "e-soccer", "cyber", "e-football", 
    "esports", "fifa", "pes", "efootball",
    "virtual", "simulated", "gtworld", "baller",
    "6 mins", "8 mins", "10 mins", "12 mins", 
    "15 mins", "20 mins", "30 mins", "h2h gg",
    # Donne
    "women", "woman", "w)", "(w", "feminine", "femminile", "donne",
    # Indonesia
    "indonesia", "indonesian",
]

# Spagna: SOLO La Liga
SPAIN_ALLOWED_LEAGUES = [
    "la liga", "laliga", "primera division", "primera división"
]

def is_excluded_league(league_name: str) -> bool:
    """Verifica se la lega è da escludere"""
    league_lower = league_name.lower()
    
    # Check keywords
    for keyword in LEAGUE_EXCLUDE_KEYWORDS:
        if keyword.lower() in league_lower:
            return True
    
    # Spagna: SOLO La Liga
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
        
        # HT recovery tracking
        self.sent_ht_alert = False
        self.ht_result = None  # 'won', 'lost', 'pending'
        self.sent_ft_recovery = False  # Ha già mandato recovery FT?

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
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("⚠️ Telegram non configurato")
        return False
    
    for attempt in range(2):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            r = requests.post(
                url, 
                data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, 
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
    """Verifica risultato segnale"""
    if signal["status"] != "pending":
        return signal["status"]
    
    bet_type = signal.get("bet_type", "")
    
    # OVER 1.5 HT
    if "HT" in bet_type or "PRIMO TEMPO" in bet_type:
        if current_minute >= 45:
            if ht_score is not None:
                ht_total = ht_score[0] + ht_score[1]
                return "won" if ht_total >= 2 else "lost"
            elif current_minute > 45:
                return "lost"
        return "pending"
    
    # OVER 2.5 FT
    elif "FT" in bet_type or "FINALE" in bet_type:
        if current_minute < 90:
            return "pending"
        
        final_total = current_score[0] + current_score[1]
        return "won" if final_total >= 3 else "lost"
    
    return "pending"

def send_daily_report():
    """Invia report giornaliero"""
    if not ENABLE_DAILY_STATS:
        return
    
    if daily_stats.total_sent == 0:
        msg = (
            f"📊 <b>REPORT GIORNALIERO</b> 📊\n"
            f"📅 {daily_stats.reset_date}\n\n"
            f"📭 Nessun segnale inviato oggi\n\n"
            f"🔄 Statistiche resettate"
        )
        send_telegram_message(msg)
        logger.info("📊 Report: 0 segnali")
        daily_stats.last_report_sent = time.time()
        return
    
    # Dividi per tipo
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
    
    # OVER 1.5 HT
    if ht_signals:
        msg += (
            f"⏰ <b>OVER 1.5 HT ({len(ht_signals)})</b>\n"
            f"✅ {ht_won} | ❌ {ht_lost} | ⏳ {ht_pending}\n"
            f"📈 WR: {ht_wr:.1f}%\n\n"
        )
        
        won_ht = [s for s in ht_signals if s["status"] == "won"]
        if won_ht:
            msg += "✅ <b>Vincenti HT:</b>\n"
            for i, s in enumerate(won_ht[:5], 1):
                score = s.get("final_score", (0, 0))
                msg += f"{i}. {s['home']} vs {s['away']}\n"
            if len(won_ht) > 5:
                msg += f"   ... e altri {len(won_ht) - 5}\n"
            msg += "\n"
        
        lost_ht = [s for s in ht_signals if s["status"] == "lost"]
        if lost_ht:
            msg += "❌ <b>Perdenti HT:</b>\n"
            for i, s in enumerate(lost_ht[:5], 1):
                msg += f"{i}. {s['home']} vs {s['away']}\n"
            if len(lost_ht) > 5:
                msg += f"   ... e altri {len(lost_ht) - 5}\n"
            msg += "\n"
    
    # OVER 2.5 FT
    if ft_signals:
        msg += (
            f"🎯 <b>OVER 2.5 FT ({len(ft_signals)})</b>\n"
            f"✅ {ft_won} | ❌ {ft_lost} | ⏳ {ft_pending}\n"
            f"📈 WR: {ft_wr:.1f}%\n\n"
        )
        
        won_ft = [s for s in ft_signals if s["status"] == "won"]
        if won_ft:
            msg += "✅ <b>Vincenti FT:</b>\n"
            for i, s in enumerate(won_ft[:5], 1):
                score = s.get("final_score", (0, 0))
                msg += f"{i}. {s['home']} vs {s['away']}\n"
            if len(won_ft) > 5:
                msg += f"   ... e altri {len(won_ft) - 5}\n"
            msg += "\n"
        
        lost_ft = [s for s in ft_signals if s["status"] == "lost"]
        if lost_ft:
            msg += "❌ <b>Perdenti FT:</b>\n"
            for i, s in enumerate(lost_ft[:5], 1):
                msg += f"{i}. {s['home']} vs {s['away']}\n"
            if len(lost_ft) > 5:
                msg += f"   ... e altri {len(lost_ft) - 5}\n"
            msg += "\n"
    
    msg += "🔄 Statistiche resettate"
    
    send_telegram_message(msg)
    logger.info("📊 Report: %d segnali | HT: %d/%d/%d | FT: %d/%d/%d", 
               daily_stats.total_sent, ht_won, ht_lost, ht_pending,
               ft_won, ft_lost, ft_pending)
    daily_stats.last_report_sent = time.time()

def get_live_events():
    """Recupera eventi live dall'API"""
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
        
        # Filtri
        if is_excluded_league(league):
            continue
        
        # In play?
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
        
        # HT score
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
        
        # Red card
        rc_a = score_a.get("rc", 0)
        rc_b = score_a.get("rc", 0)
        has_red_card = False
        try:
            if int(rc_a) > 0 or int(rc_b) > 0:
                has_red_card = True
        except:
            pass
        
        # Odds
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
    
    # Daily report
    if ENABLE_DAILY_STATS:
        current_hour = datetime.now().hour
        if current_hour == DAILY_REPORT_HOUR:
            if (now - daily_stats.last_report_sent) > 3600:
                send_daily_report()
                daily_stats.reset()
    
    # Cooldown check
    if _last_daily_429_ts > 0:
        elapsed = (now - _last_daily_429_ts) / 60
        if elapsed < COOLDOWN_ON_DAILY_429_MIN:
            return
    
    events = get_live_events()
    
    # Update stats tracking
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
        
        # Track 0-0
        if cur_score == (0, 0):
            zero_zero_count += 1
        
        # Init state
        if eid not in match_state:
            match_state[eid] = MatchState()
            match_state[eid].first_seen_score = cur_score
        
        st = match_state[eid]
        
        # Red card check
        if has_red_card:
            if not st.red_card_detected:
                st.red_card_detected = True
                logger.info("🟥 ROSSO: %s vs %s - SCARTATO", home, away)
                st.notified = True
            continue
        
        if st.red_card_detected:
            continue
        
        # Check HT result for recovery
        if st.sent_ht_alert and not st.sent_ft_recovery:
            if current_minute >= 45 and ht_score is not None:
                ht_total = ht_score[0] + ht_score[1]
                
                if ht_total < 2:
                    # HT LOST → Send OVER 2.5 FT
                    st.ht_result = "lost"
                    st.sent_ft_recovery = True
                    
                    # Check quote disponibili
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
                                logger.info("✅ RECOVERY FT %d': %s vs %s | %.2f | OVER 2.5 FT", 
                                           current_minute, home, away, scorer_price)
                                
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
        
        # Skip if already notified
        if st.notified:
            continue
        
        monitored_count += 1
        
        # STEP 1: Detect goal
        if st.goal_time is None:
            if cur_score in [(1, 0), (0, 1)] and st.first_seen_score == (0, 0):
                # Conferma goal (2-loop)
                if st.pending_goal_score == cur_score:
                    st.pending_goal_count += 1
                    logger.info("⏳ Goal rilevato (%d/2): %s vs %s (%d-%d)",
                               st.pending_goal_count, home, away, cur_score[0], cur_score[1])
                    
                    if st.pending_goal_count >= 2:
                        st.goal_time = now
                        st.goal_minute = current_minute
                        st.scoring_team = "home" if cur_score == (1, 0) else "away"
                        logger.info("⚽ GOAL CONFERMATO %d': %s vs %s (%d-%d) | %s", 
                                   current_minute, home, away, cur_score[0], cur_score[1], league)
                else:
                    if st.pending_goal_score is not None:
                        logger.info("❌ Falso positivo annullato: %s vs %s", home, away)
                    st.pending_goal_score = cur_score
                    st.pending_goal_count = 1
            
            continue
        
        # STEP 2: Wait after goal
        if (now - st.goal_time) < WAIT_AFTER_GOAL_SEC:
            continue
        
        # STEP 3: No odds
        if home_price is None and away_price is None:
            st.consecutive_errors += 1
            if st.consecutive_errors > 8:
                logger.warning("⚠️ Skip %s vs %s (no odds)", home, away)
                st.notified = True
            continue
        
        # STEP 4: Get scorer price
        scorer_price = home_price if st.scoring_team == "home" else away_price
        
        if scorer_price is None:
            st.consecutive_errors += 1
            if st.consecutive_errors > 8:
                logger.warning("⚠️ Skip %s vs %s (no odds)", home, away)
                st.notified = True
            continue
        
        st.consecutive_errors = 0
        
        # STEP 5: Baseline
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
        
        # STEP 6: Monitor
        delta = scorer_price - st.baseline
        st.last_quote = scorer_price
        
        if delta >= MIN_RISE * 0.7:
            logger.info("📈 %d' | %s vs %s: %.2f (base %.2f, Δ+%.3f)", 
                       current_minute, home, away, scorer_price, st.baseline, delta)
        
        # STEP 7: Alert OVER 1.5 HT (if within 25')
        if scorer_price > MAX_FINAL_QUOTE:
            logger.warning("⚠️ Quota %.2f > %.2f: %s vs %s - SCARTATO", 
                          scorer_price, MAX_FINAL_QUOTE, home, away)
            st.notified = True
            continue
        
        # 🔥 Check: cambio quote entro 25'?
        if current_minute > GOAL_MINUTE_MAX_HT:
            logger.info("⏭️ Cambio quote %d' > %d': %s vs %s - SKIP",
                       current_minute, GOAL_MINUTE_MAX_HT, home, away)
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
                logger.info("✅ ALERT HT %d': %s vs %s | Goal %d' | %.2f→%.2f (+%.2f) | OVER 1.5 HT", 
                           current_minute, home, away, st.goal_minute, st.baseline, scorer_price, delta)
                
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
    
    # Stats log
    if _loop % 15 == 0:
        logger.info("📊 %d live | %d 0-0 | %d monitored | Stats: %d sent, %d won, %d lost", 
                   len(events), zero_zero_count, monitored_count,
                   daily_stats.total_sent, daily_stats.total_won, daily_stats.total_lost)

def main():
    logger.info("="*60)
    logger.info("🚀 BOT HT RECOVERY v8.0")
    logger.info("="*60)
    logger.info("⚙️  Config:")
    logger.info("   • Goal → OVER 1.5 HT (cambio quote ≤%d')", GOAL_MINUTE_MAX_HT)
    logger.info("   • HT perso → OVER 2.5 FT (recovery)")
    logger.info("   • Quote: %.2f-%.2f | Max: %.2f", BASELINE_MIN, BASELINE_MAX, MAX_FINAL_QUOTE)
    logger.info("   • Rise: +%.2f | Stake: €%d", MIN_RISE, STAKE)
    logger.info("   • Filters: Spain (La Liga only), Women, Indonesia")
    logger.info("   • Report: %02d:00", DAILY_REPORT_HOUR)
    logger.info("="*60)
    
    send_telegram_message(
        f"🤖 <b>Bot HT RECOVERY</b> v8.0 ⚡\n\n"
        f"⚽ <b>STRATEGIA:</b>\n"
        f"1️⃣ Cambio quote ≤{GOAL_MINUTE_MAX_HT}' → <b>OVER 1.5 HT</b>\n"
        f"2️⃣ Se HT perso → <b>OVER 2.5 FT</b>\n\n"
        f"📊 Quote: {BASELINE_MIN:.2f}-{BASELINE_MAX:.2f}\n"
        f"📈 Rise: +{MIN_RISE:.2f} | Max: {MAX_FINAL_QUOTE:.2f}\n"
        f"💰 Stake: €{STAKE}\n\n"
        f"🛡️ Filtri attivi\n"
        f"📊 Report: 00:00\n\n"
        f"🔍 Monitoraggio attivo!"
    )
    
    while True:
        try:
            main_loop()
        except KeyboardInterrupt:
            logger.info("⛔ Interrupted")
            break
        except Exception as e:
            logger.error("❌ Error: %s", e, exc_info=True)
        
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
