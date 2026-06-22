import hashlib
import pyotp
import requests
import re
from playwright.sync_api import sync_playwright

# --- CONFIG ---
API_KEY     = "08b58d70ff1e4d21899f47e41650ab8c"
API_SECRET  = "2026.d9a4e6eb576b4464b1b433cf9900343eb7d0b494b27647fb"
USER_ID     = "FZ38545"
PASSWORD    = "Chand@7536"
TOTP_SECRET = "3C2SIIEUCRI22U56ZUGE47B6OOZF466G"

def get_flattrade_jkey():
    print("Initializing Playwright with Stealth Settings...", flush=True)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--no-zygote',
                '--disable-blink-features=AutomationControlled'  # Hide automation flag
            ]
        )
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        
        page = context.new_page()

        # Overwrite the navigator.webdriver property completely to bypass Cloudflare
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
            
            # Click and fill natively
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
            
            # Find the submit button and dispatch a real human click event
            submit_btn = page.locator('button:has-text("Log In"), button:has-text("Sign In")').first
            submit_btn.dispatch_event("click")

            print("Step 3: Waiting for background redirect...", flush=True)
            for _ in range(40):
                if captured_code["code"]:
                    break
                page.wait_for_timeout(500)

            if not captured_code["code"]:
                print(f"❌ Timed out. Still at: {page.url}", flush=True)
                
                print("\n--- VISIBLE PAGE TEXT ---")
                try:
                    body_text = page.locator("body").text_content()
                    for line in body_text.split('\n'):
                        if line.strip():
                            print(f"| {line.strip()}")
                except Exception as e:
                    print(f"Could not read page text: {e}")
                print("-------------------------\n")
                
                page.screenshot(path="stuck_debug.png")
                return None

            print(f"✅ Success! Captured Code: {captured_code['code']}", flush=True)
            return captured_code['code']

        except Exception as e:
            print(f"❌ Playwright error: {e}", flush=True)
            return None
        finally:
            browser.close()

# --- RUN ---
jkey = get_flattrade_jkey()
if jkey:
    code = jkey
    api_token = hashlib.sha256((API_KEY + code + API_SECRET).encode()).hexdigest()
    resp = requests.post('https://authapi.flattrade.in/trade/apitoken',
                        json={'api_key': API_KEY, 'request_code': code, 'api_secret': api_token})
    result = resp.json()
    print("\n--- FLATTRADE RESPONSE ---", flush=True)
    print(result, flush=True)
    print("--------------------------\n", flush=True)
else:
    print("failed to get code in the first place", flush=True)
