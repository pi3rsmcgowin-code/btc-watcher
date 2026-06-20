# BTC funding-reversion -> email on LONG setup. Runs on GitHub Actions (cloud).
# Pure stdlib. Credentials come from env (GitHub Secrets). State persists in state.json.
import os, json, time, ssl, smtplib, urllib.request
from email.message import EmailMessage

THR_Q, HOLD_H, STATE = 0.05, 40, "state.json"

def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "btc-watcher"})
    with urllib.request.urlopen(req, timeout=30) as r:
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

def quantile(sv, q):
    n = len(sv)
    if n == 0: return float("nan")
    i = (n - 1) * q; lo = int(i); hi = min(lo + 1, n - 1)
    return sv[lo] + (sv[hi] - sv[lo]) * (i - lo)

def send_email(subject, body):
    cfg = {"to": os.getenv("EMAIL_TO"), "from": os.getenv("EMAIL_FROM"), "pw": os.getenv("EMAIL_PASS"),
           "host": os.getenv("SMTP_HOST", "smtp.gmail.com"), "port": int(os.getenv("SMTP_PORT", "587"))}
    if not all([cfg["to"], cfg["from"], cfg["pw"]]):
        raise RuntimeError("missing EMAIL_TO / EMAIL_FROM / EMAIL_PASS secrets")
    m = EmailMessage(); m["From"] = cfg["from"]; m["To"] = cfg["to"]; m["Subject"] = subject; m.set_content(body)
    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as s:
        s.starttls(context=ssl.create_default_context()); s.login(cfg["from"], cfg["pw"]); s.send_message(m)

def main():
    # manual "Run workflow" click -> send a confirmation so you know it's wired
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        try: send_email("BTC watcher: live", "Your cloud watcher is set up on GitHub Actions and running. You'll get an email when funding flips to a LONG setup (bottom 5%)."); print("confirmation email sent")
        except Exception as e: print("confirmation email FAILED:", e)

    fund = get_funding()
    if not fund:
        print("no funding data this run; skipping"); return
    rates = sorted(r for _, r in fund); thr = quantile(rates, THR_Q); cur = fund[-1][1]
    is_long = cur <= thr
    print(f"funding {cur*100:+.4f}%  thr {thr*100:+.4f}%  -> {'LONG SETUP' if is_long else 'wait'}")

    state = {"last": "OTHER"}
    if os.path.exists(STATE):
        try: state = json.load(open(STATE))
        except Exception: pass

    if is_long and state.get("last") != "LONG":
        body = (f"BTC funding-reversion: LONG setup.\n\n"
                f"Funding {cur*100:+.4f}% is at/below the bottom-5% threshold {thr*100:+.4f}% (crowded shorts).\n"
                f"Rule: go LONG, hold ~{HOLD_H}h, size 1-2x.\n\n"
                f"Checked {time.strftime('%Y-%m-%d %H:%M UTC')} (OKX).")
        try: send_email("BTC LONG setup (funding bottom 5%)", body); print("ALERT email sent")
        except Exception as e: print("alert email FAILED:", e)

    state["last"] = "LONG" if is_long else "OTHER"
    json.dump(state, open(STATE, "w"))

if __name__ == "__main__":
    main()
