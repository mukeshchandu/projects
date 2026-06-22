#generate_token.py — reads creds from .env, writes FLATTRADE_SESSION_TOKEN back to .env
import re, sys, hashlib
import pyotp, requests
from playwright.sync_api import sync_playwright

ENV_PATH = ".env"


def read_env(path=ENV_PATH):
    env = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def set_env(key, value, path=ENV_PATH):
    lines = open(path).read().splitlines()
    out, found = [], False
    for ln in lines:
        if ln.strip().startswith(f"{key}="):
            out.append(f"{key}={value}"); found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    open(path, "w").write("\n".join(out) + "\n")


cfg = read_env()
def need(k):
    v = cfg.get(k)
    if not v:
        sys.exit(f"ERROR: {k} missing from .env")
    return v

API_KEY     = need("FLATTRADE_API_KEY")
API_SECRET  = need("FLATTRADE_API_SECRET")
PASSWORD    = need("PASSWORD")
TOTP_SECRET = need("TOTP_SECRET")
USER_ID     = cfg.get("FLATTRADE_USER_ID") or cfg.get("USER_ID") or cfg.get("FLATTRADE_UID")
if not USER_ID:
    sys.exit("ERROR: USER_ID missing from .env — add a line:  USER_ID=FZ38545")


def get_request_code():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
            "--disable-gpu", "--no-zygote", "--disable-blink-features=AutomationControlled",
        ])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        captured = {"code": None}
        def on_request(req):
            if "code=" in req.url:
                m = re.search(r"code=([a-zA-Z0-9-]+)", req.url)
                if m:
                    captured["code"] = m.group(1)
        page.on("request", on_request)

        try:
            page.goto(f"https://auth.flattrade.in/?app_key={API_KEY}",
                      wait_until="networkidle", timeout=30000)
            u = page.locator('input[placeholder*="User ID"]'); u.wait_for(timeout=5000)
            u.click(); u.fill(USER_ID)
            pw = page.locator('input[placeholder*="Password"]'); pw.click(); pw.fill(PASSWORD)
            secret = re.sub(r"[^A-Z2-7]", "", TOTP_SECRET.upper())
            otp = pyotp.TOTP(secret).now()
            of = page.locator('input[placeholder*="OTP"]'); of.click(); of.fill(otp)
            page.wait_for_timeout(1000)
            page.locator('button:has-text("Log In"), button:has-text("Sign In")').first.dispatch_event("click")
            for _ in range(40):
                if captured["code"]:
                    break
                page.wait_for_timeout(500)
            if not captured["code"]:
                page.screenshot(path="stuck_debug.png")
                print(f"timed out at {page.url} (see stuck_debug.png)", file=sys.stderr)
            return captured["code"]
        finally:
            browser.close()


def main():
    print("Logging in to Flattrade (headless)...", flush=True)
    code = get_request_code()
    if not code:
        sys.exit("ERROR: failed to capture request code")
    print(f"Got code ({code[:4]}...). Exchanging for token...", flush=True)

    api_hash = hashlib.sha256((API_KEY + code + API_SECRET).encode()).hexdigest()
    r = requests.post("https://authapi.flattrade.in/trade/apitoken",
                      json={"api_key": API_KEY, "request_code": code, "api_secret": api_hash},
                      timeout=15)
    token = r.json().get("token")
    if not token:
        sys.exit(f"ERROR: token exchange failed - {r.json()}")

    set_env("FLATTRADE_SESSION_TOKEN", token)
    print(f"OK: FLATTRADE_SESSION_TOKEN updated in .env  ({token[:4]}...{token[-4:]})", flush=True)


if __name__ == "__main__":
    main()
