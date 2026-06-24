import hashlib
import pyotp
import requests
import re
from playwright.sync_api import sync_playwright

def read_env(path=".env"):
    env = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def set_env(key, value, path=".env"):
    lines = open(path).read().splitlines()
    out, found = [], False
    for ln in lines:
        if re.match(rf"^\s*{re.escape(key)}\s*=", ln):
            out.append(f"{key}={value}"); found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    open(path, "w").write("\n".join(out) + "\n")

cfg = read_env()
API_KEY     = cfg["FLATTRADE_API_KEY"]
API_SECRET  = cfg["FLATTRADE_API_SECRET"]
USER_ID     = cfg["FLATTRADE_USER_ID"]
PASSWORD    = cfg["PASSWORD"]
TOTP_SECRET = cfg["TOTP_SECRET"]

def get_flattrade_jkey():
    print("Initializing Playwright...", flush=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',
                  '--disable-gpu','--no-zygote','--disable-blink-features=AutomationControlled']
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        captured_code = {"code": None}

        def on_request(request):
            if "code=" in request.url:
                match = re.search(r"code=([a-zA-Z0-9-]+)", request.url)
                if match:
                    captured_code["code"] = match.group(1)

        page.on("request", on_request)
        try:
            print("Step 1: Navigating to Portal...", flush=True)
            page.goto(f"https://auth.flattrade.in/?app_key={API_KEY}", wait_until="networkidle", timeout=30000)
            print("Step 2: Entering Credentials...", flush=True)
            user_field = page.locator('input[placeholder*="User ID"]')
            user_field.wait_for(timeout=5000)
            user_field.click()
            user_field.fill(USER_ID)
            pass_field = page.locator('input[placeholder*="Password"]')
            pass_field.click()
            pass_field.fill(PASSWORD)
            totp = pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now()
            otp_field = page.locator('input[placeholder*="OTP"]')
            otp_field.click()
            otp_field.fill(totp)
            print(f"Submitting with TOTP: {totp}", flush=True)
            page.wait_for_timeout(1000)
            submit_btn = page.locator('button:has-text("Log In"), button:has-text("Sign In")').first
            submit_btn.dispatch_event("click")
            print("Step 3: Waiting for redirect...", flush=True)
            for _ in range(40):
                if captured_code["code"]:
                    break
                page.wait_for_timeout(500)
            if not captured_code["code"]:
                print(f"Timed out. Still at: {page.url}", flush=True)
                try:
                    body_text = page.locator("body").text_content()
                    for line in body_text.split('\n'):
                        if line.strip():
                            print(f"| {line.strip()}")
                except Exception as e:
                    print(f"Could not read page text: {e}")
                page.screenshot(path="stuck_debug.png")
                return None
            print(f"Success! Captured Code: {captured_code['code']}", flush=True)
            return captured_code['code']
        except Exception as e:
            print(f"Playwright error: {e}", flush=True)
            return None
        finally:
            browser.close()

jkey = get_flattrade_jkey()
if jkey:
    code = jkey
    api_token = hashlib.sha256((API_KEY + code + API_SECRET).encode()).hexdigest()
    resp = requests.post('https://authapi.flattrade.in/trade/apitoken',
                        json={'api_key': API_KEY, 'request_code': code, 'api_secret': api_token})
    result = resp.json()
    print("\n--- FLATTRADE RESPONSE ---", flush=True)
    print(result, flush=True)
    token = result.get("token") or result.get("jKey") or result.get("access_token")
    if token:
        set_env("FLATTRADE_SESSION_TOKEN", token)
        print(f"Token saved to .env", flush=True)
    else:
        print(f"No token in response: {result}", flush=True)
else:
    print("Failed to get code", flush=True)
