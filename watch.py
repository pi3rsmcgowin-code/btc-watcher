# BTC funding-reversion -> entry email, AI+market-state exit email, paper-trade log,
# and (optional) Buttondown broadcast to your subscribers. Runs on GitHub Actions. Pure stdlib.
import os, json, time, ssl, smtplib, urllib.request
from email.message import EmailMessage
 
THR_Q, STATE, COST = 0.05, "state.json", 0.0007
MIN_H, DEFAULT_H, HARD_MAX_H = 8, 40, 60
DISCLAIMER = "\n\n— — —\nExperimental & paper-tested signal. NOT financial advice. Do your own research; you trade at your own risk."
 
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
 
def broadcast(subject, body):
    """Send to all Buttondown subscribers (optional; skipped if no key set)."""
    key = os.getenv("BUTTONDOWN_API_KEY")
    if not key: return
    data = json.dumps({"subject": subject, "body": body + DISCLAIMER, "status": "about_to_send"}).encode()
    try:
        r = http_json("https://api.buttondown.email/v1/emails", data=data,
                      headers={"Authorization": "Token " + key, "Content-Type": "application/json"})
        print("broadcast queued:", r.get("id", "ok"))
    except Exception as e:
        print("broadcast failed:", str(e)[:180])
 
def ai_call(ctx, key):
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001", "max_tokens": 120,
        "system": ("Exit assistant for a BTC funding-reversion LONG. Edge: enter when perp funding is in its bottom 5% "
                   "(crowded shorts); profit as shorts unwind and price reverts up, plus negative-funding carry. Thesis "
                   "is DONE when funding has normalized to/above its median (shorts unwound) or the bounce is "
                   'mature/stalling. Tested default hold ~40h. Reply ONLY compact JSON: '
                   '{"action":"EXIT"|"HOLD","reason":"<=12 words"}'),
        "messages": [{"role": "user", "content": ctx}],
    }).encode()
    j = http_json("https://api.anthropic.com/v1/messages", data=body,
                  headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    txt = j["content"][0]["text"]; a, b = txt.find("{"), txt.rfind("}")
    d = json.loads(txt[a:b + 1]); return d["action"].upper(), d.get("reason", "")
 
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
 
def carry_between(fund, t0, t1):
    return -sum(rate for ts, rate in fund if t0 <= ts < t1)
 
def apply_paper_exit(paper, tr, exit_px, exit_ts, fund):
    pr = exit_px / tr["entry_price"] - 1
    car = carry_between(fund, tr["entry_ts"], exit_ts)
    net = pr + car - COST
    paper["equity"] *= (1 + net); paper["n"] += 1; paper["wins"] += 1 if net > 0 else 0
    rec = {"entry": time.strftime("%Y-%m-%d %H:%M", time.gmtime(tr["entry_ts"] / 1000)),
           "exit": time.strftime("%Y-%m-%d %H:%M", time.gmtime(exit_ts / 1000)),
           "held_h": round((exit_ts - tr["entry_ts"]) / 3600000, 1),
           "entry_px": round(tr["entry_price"]), "exit_px": round(exit_px),
           "price_pct": round(pr * 100, 2), "carry_pct": round(car * 100, 3),
           "net_pct": round(net * 100, 2), "equity": round(paper["equity"], 4)}
    paper["trades"].append(rec); return rec
 
def summary_line(paper, price):
    n, eq = paper["n"], paper["equity"]; wr = (paper["wins"] / n * 100) if n else 0
    bh = (price / paper["first_price"] - 1) * 100 if paper.get("first_price") else 0
    return f"PAPER (1x): {n} trades, {wr:.0f}% win, total {(eq-1)*100:+.1f}% | BTC buy&hold since start {bh:+.1f}%"
 
def write_paper_files(paper, price):
    csv = "entry,exit,held_h,entry_px,exit_px,price_pct,carry_pct,net_pct,equity\n" + "\n".join(
        f"{t['entry']},{t['exit']},{t['held_h']},{t['entry_px']},{t['exit_px']},{t['price_pct']},{t['carry_pct']},{t['net_pct']},{t['equity']}"
        for t in paper["trades"])
    open("paper_trades.csv", "w").write(csv + "\n")
    md = ["# Paper-trading record (1x, fee+carry modeled)", "", f"_{summary_line(paper, price)}_", "",
          "| entry (UTC) | exit | held h | entry $ | exit $ | price % | carry % | net % | equity x |",
          "|---|---|---|---|---|---|---|---|---|"]
    for t in paper["trades"][-30:]:
        md.append(f"| {t['entry']} | {t['exit']} | {t['held_h']} | {t['entry_px']:,} | {t['exit_px']:,} | "
                  f"{t['price_pct']:+} | {t['carry_pct']:+} | {t['net_pct']:+} | {t['equity']} |")
    md += ["", "_Net % = price move + funding carry − 0.07% cost. Deployed (AI-exit) logic, paper only. Not financial advice._"]
    open("paper_log.md", "w").write("\n".join(md) + "\n")
 
def main():
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        try: send_email("BTC watcher: live", "Cloud watcher running: entry + AI-exit alerts + paper log + subscriber broadcasts active.")
        except Exception as e: print("confirmation email FAILED:", e)
 
    fund = get_funding()
    if not fund: print("no funding data; skip"); return
    rates = sorted(r for _, r in fund); thr = quantile(rates, THR_Q); med = quantile(rates, 0.5); cur = fund[-1][1]
    closes = get_closes(); price = closes[-1] if closes else None
    if not price: print("no price; skip"); return
    is_long = cur <= thr; now = int(time.time() * 1000)
    print(f"funding {cur*100:+.4f}%  thr {thr*100:+.4f}%  med {med*100:+.4f}%  -> {'LONG SETUP' if is_long else 'wait'}")
 
    state = {"last": "OTHER", "trade": None, "paper": None}
    if os.path.exists(STATE):
        try: state.update(json.load(open(STATE)))
        except Exception: pass
    trade = state.get("trade")
    paper = state.get("paper") or {"equity": 1.0, "n": 0, "wins": 0, "trades": [], "first_price": None, "first_ts": None}
 
    # ENTRY
    if is_long and state.get("last") != "LONG" and trade is None:
        if paper["first_price"] is None: paper["first_price"], paper["first_ts"] = price, now
        body = (f"BTC LONG entry.\nFunding {cur*100:+.4f}% <= 5th-pct {thr*100:+.4f}% (crowded shorts). Entry ~${price:,.0f}. "
                f"Rule: go LONG, size 1-2x. Exit alert comes when AI + market state say so (fallback 40h).\n\n{summary_line(paper, price)}")
        try: send_email("BTC LONG entry (funding bottom 5%)", body); print("ENTRY email sent")
        except Exception as e: print("entry email FAILED:", e)
        broadcast("BTC: LONG setup now",
                  f"Bitcoin funding just dropped into its bottom 5% (crowded shorts) at ~${price:,.0f} — the historical long-setup signal. "
                  f"The plan is a long held ~40h, then exit. A follow-up email will say when to close.")
        trade = {"entry_ts": now, "entry_price": price}
    state["last"] = "LONG" if is_long else "OTHER"
 
    # EXIT (AI + guardrails) + paper bookkeeping + broadcast
    if trade:
        held_h = (now - trade["entry_ts"]) / 3600000.0
        pnl = (price - trade["entry_price"]) / trade["entry_price"]
        r = rsi(closes); mom = (price / closes[-7] - 1) if len(closes) >= 7 else 0.0
        key = os.getenv("ANTHROPIC_API_KEY")
        ctx = (f"LONG BTC, held {held_h:.1f}h, entry ${trade['entry_price']:,.0f}, now ${price:,.0f}, unrealized {pnl*100:+.2f}%.\n"
               f"Funding {cur*100:+.4f}% (trigger {thr*100:+.4f}%, median {med*100:+.4f}%); "
               f"{'normalized: shorts unwound' if cur >= med else 'still below median: shorts crowded'}.\n"
               f"RSI(1h) {r:.0f}, last-6h {mom*100:+.2f}%. EXIT or HOLD?")
        thunk = (lambda: ai_call(ctx, key)) if key else None
        action, reason = decide_exit(held_h, thunk)
        print(f"  in trade {held_h:.1f}h pnl {pnl*100:+.2f}% -> {action} ({reason})")
        if action == "EXIT":
            rec = apply_paper_exit(paper, trade, price, now, fund)
            body = (f"EXIT BTC long.\nReason: {reason}. Held {rec['held_h']}h, entry ${rec['entry_px']:,}, now ${rec['exit_px']:,}, "
                    f"net {rec['net_pct']:+}% (incl carry/cost).\n\n{summary_line(paper, price)}\n\nSee paper_log.md in your repo.")
            try: send_email("BTC EXIT long now", body); print("EXIT email sent")
            except Exception as e: print("exit email FAILED:", e)
            broadcast("BTC: close the long",
                      f"Time to close the Bitcoin long from the last alert. Now ~${price:,.0f}; this trade's paper result {rec['net_pct']:+}% "
                      f"(price move + funding − costs). Running paper record: {summary_line(paper, price)}.")
            trade = None
 
    state["trade"] = trade; state["paper"] = paper
    json.dump(state, open(STATE, "w"))
    write_paper_files(paper, price)
 
if __name__ == "__main__":
    main()
 
