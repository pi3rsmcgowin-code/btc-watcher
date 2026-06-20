# BTC funding-reversion -> email on LONG entry, and an AI+market-state EXIT email.
# Runs on GitHub Actions. Pure stdlib. Creds & API key from env (GitHub Secrets).
# State persists in state.json. The TESTED 40h exit is the fallback floor.
import os, json, time, ssl, smtplib, urllib.request
from email.message import EmailMessage

THR_Q, STATE = 0.05, "state.json"
MIN_H, DEFAULT_H, HARD_MAX_H = 8, 40, 60        # exit guardrails

def http_json(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": "btc-watcher"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode())

def get_funding():
    after, fund = int(time.time() * 1000), []
    for _ in range(6):
        j = http_json(f"https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=100&after={after}")
        d = j.get("data", [])
        if not d: break
        for r in d: fund.append((int(r["fundingTime"]), float(r["fundingRate"])))
        after = min(int(r["fundingTime"]) for r in d) - 1
        time.sleep(0.1)
    fund.sort()
    return fund

def get_closes():
    j = http_json("https://www.okx.com/api/v5/market/candles?instId=BTC-USDT-SWAP&bar=1H&limit=120")
    rows = sorted(([int(r[0]), float(r[4])] for r in j.get("data", [])), key=lambda x: x[0])
    return [c for _, c in rows]

def quantile(sv, q):
    n = len(sv)
    if n == 0: return float("nan")
    i = (n - 1) * q; lo = int(i); hi = min(lo + 1, n - 1)
    return sv[lo] + (sv[hi] - sv[lo]) * (i - lo)

def rsi(closes, n=14):
    if len(closes) <= n: return 50.0
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]; g += max(d, 0); l += max(-d, 0)
    g /= n; l /= n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n; l = (l * (n - 1) + max(-d, 0)) / n
    return 100 - 100 / (1 + g / (l or 1e-9))

def send_email(subject, body):
    cfg = {"to": os.getenv("EMAIL_TO"), "from": os.getenv("EMAIL_FROM"), "pw": os.getenv("EMAIL_PASS"),
           "host": os.getenv("SMTP_HOST", "smtp.gmail.com"), "port": int(os.getenv("SMTP_PORT", "587"))}
    if not all([cfg["to"], cfg["from"], cfg["pw"]]):
        raise RuntimeError("missing EMAIL_TO / EMAIL_FROM / EMAIL_PASS secrets")
    m = EmailMessage(); m["From"] = cfg["from"]; m["To"] = cfg["to"]; m["Subject"] = subject; m.set_content(body)
    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as s:
        s.starttls(context=ssl.create_default_context()); s.login(cfg["from"], cfg["pw"]); s.send_message(m)

def ai_call(ctx, key):
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001", "max_tokens": 120,
        "system": ("Exit assistant for a BTC funding-reversion LONG. Edge: enter when perp funding is in its "
                   "bottom 5% (crowded shorts); profit as shorts unwind and price reverts up, plus negative-funding "
                   "carry. Thesis is DONE when funding has normalized to/above its median (shorts unwound) or the "
                   "bounce is mature/stalling. Tested default hold ~40h. Reply ONLY compact JSON: "
                   '{"action":"EXIT"|"HOLD","reason":"<=12 words"}'),
        "messages": [{"role": "user", "content": ctx}],
    }).encode()
    j = http_json("https://api.anthropic.com/v1/messages", data=body,
                  headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    txt = j["content"][0]["text"]
    a, b = txt.find("{"), txt.rfind("}")
    d = json.loads(txt[a:b + 1])
    return d["action"].upper(), d.get("reason", "")

def decide_exit(held_h, ai_thunk):
    if held_h < MIN_H: return "HOLD", "min hold"
    if held_h >= HARD_MAX_H: return "EXIT", f"hard cap {HARD_MAX_H}h reached"
    if ai_thunk is not None:
        try:
            a, r = ai_thunk()
            if a in ("EXIT", "HOLD"): return a, r
        except Exception as e:
            print("AI exit call failed:", str(e)[:120])
    return ("EXIT", f"{DEFAULT_H}h tested hold complete") if held_h >= DEFAULT_H else ("HOLD", "<40h")

def main():
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        try: send_email("BTC watcher: live", "Cloud watcher is running. Entry + AI-exit alerts active.")
        except Exception as e: print("confirmation email FAILED:", e)

    fund = get_funding()
    if not fund: print("no funding data; skip"); return
    rates = sorted(r for _, r in fund); thr = quantile(rates, THR_Q); med = quantile(rates, 0.5); cur = fund[-1][1]
    closes = get_closes(); price = closes[-1] if closes else None
    is_long = cur <= thr
    now = int(time.time() * 1000)
    print(f"funding {cur*100:+.4f}%  thr {thr*100:+.4f}%  med {med*100:+.4f}%  -> {'LONG SETUP' if is_long else 'wait'}")

    state = {"last": "OTHER", "trade": None}
    if os.path.exists(STATE):
        try: state.update(json.load(open(STATE)))
        except Exception: pass
    trade = state.get("trade")

    # ---- ENTRY ----
    if is_long and state.get("last") != "LONG" and trade is None and price:
        body = (f"BTC funding-reversion: LONG setup.\n\nFunding {cur*100:+.4f}% <= bottom-5% threshold {thr*100:+.4f}% "
                f"(crowded shorts).\nEntry ~${price:,.0f}. Rule: go LONG, size 1-2x. You'll get an EXIT email when the "
                f"AI + market state say so (fallback 40h).\n\n{time.strftime('%Y-%m-%d %H:%M UTC')}")
        try: send_email("BTC LONG entry (funding bottom 5%)", body); print("ENTRY email sent")
        except Exception as e: print("entry email FAILED:", e)
        trade = {"entry_ts": now, "entry_price": price}
    state["last"] = "LONG" if is_long else "OTHER"

    # ---- EXIT (AI + market state, guardrailed) ----
    if trade and price:
        held_h = (now - trade["entry_ts"]) / 3600000.0
        pnl = (price - trade["entry_price"]) / trade["entry_price"]
        r = rsi(closes); mom = (price / closes[-7] - 1) if len(closes) >= 7 else 0.0
        key = os.getenv("ANTHROPIC_API_KEY")
        ctx = (f"Position: LONG BTC, held {held_h:.1f}h, entry ${trade['entry_price']:,.0f}, now ${price:,.0f}, "
               f"unrealized {pnl*100:+.2f}%.\nFunding now {cur*100:+.4f}% (entry-trigger threshold {thr*100:+.4f}%, "
               f"median {med*100:+.4f}%). Funding {'has normalized (>=median: shorts unwound)' if cur >= med else 'still below median (shorts still crowded)'}.\n"
               f"RSI(1h) {r:.0f}, last-6h move {mom*100:+.2f}%.\nShould we EXIT or HOLD this long?")
        thunk = (lambda: ai_call(ctx, key)) if key else None
        action, reason = decide_exit(held_h, thunk)
        print(f"  in trade {held_h:.1f}h  pnl {pnl*100:+.2f}%  -> {action} ({reason})")
        if action == "EXIT":
            body = (f"EXIT your BTC long now.\n\nReason: {reason}.\nHeld {held_h:.1f}h, entry ${trade['entry_price']:,.0f}, "
                    f"now ${price:,.0f}, P&L {pnl*100:+.2f}%.\nFunding {cur*100:+.4f}% (median {med*100:+.4f}%), RSI {r:.0f}.\n\n"
                    f"{time.strftime('%Y-%m-%d %H:%M UTC')}")
            try: send_email("BTC EXIT long now", body); print("EXIT email sent")
            except Exception as e: print("exit email FAILED:", e)
            trade = None

    state["trade"] = trade
    json.dump(state, open(STATE, "w"))

if __name__ == "__main__":
    main()
