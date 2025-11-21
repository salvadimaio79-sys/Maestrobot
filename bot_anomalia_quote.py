# =========================
def main():
    if not all([TELEGRAM_TOKEN, CHAT_ID, RAPIDAPI_KEY]):
        raise SystemExit("❌ Variabili mancanti")
    
    logger.info("="*60)
    logger.info("🚀 BOT QUOTE JUMP - FAST VERSION")
    logger.info("="*60)
    logger.info("⚙️  Config:")
    logger.info("   • Min rise: +%.2f", MIN_RISE)
    logger.info("   • Range: %.2f-%.2f", BASELINE_MIN, BASELINE_MAX)
    logger.info("   • Wait goal: %ds", WAIT_AFTER_GOAL_SEC)
    logger.info("   • Check: %ds", CHECK_INTERVAL)
    logger.info("   • Samples: %d (ogni %ds)", BASELINE_SAMPLES, BASELINE_SAMPLE_INTERVAL)
    logger.info("   • Max calls: %d/loop", MAX_ODDS_CALLS_PER_LOOP)
    logger.info("="*60)
    
    send_telegram_message(
        f"🤖 <b>Bot FAST V2</b> ⚡\n\n"
        f"✅ 0-0 → 1-0/0-1\n"
        f"✅ Quote {BASELINE_MIN:.2f}-{BASELINE_MAX:.2f}\n"
        f"✅ Rise <b>+{MIN_RISE:.2f}</b>\n"
        f"⚡ Wait <b>{WAIT_AFTER_GOAL_SEC}s</b> | {BASELINE_SAMPLES} samples ogni {BASELINE_SAMPLE_INTERVAL}s\n"
        f"⚡ Max {MAX_ODDS_CALLS_PER_LOOP} calls/loop\n\n"
        f"🔍 Monitoraggio attivo!"
    )
    
    main_loop()

if name == "main":
    main()
