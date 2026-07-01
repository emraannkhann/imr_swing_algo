import os
import json
import time
import requests
import pyotp
import pandas as pd
import pytz
from datetime import datetime, time as dt_time, timedelta
from urllib.parse import urlencode, urlparse, parse_qs
from playwright.sync_api import sync_playwright
from googleapiclient.discovery import build
from google.oauth2 import service_account

# ==========================================
# CONSTANTS & CONFIGURATION MATCHES
# ==========================================
IST = pytz.timezone('Asia/Kolkata')
print_logs_dt = datetime.now(IST).strftime('%Y%m%d')

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
CRED_FILE = os.path.join(BASE_DIR, "imr_resources/imr_upstox_creds.json")
TOKEN_FILE= os.path.join(BASE_DIR, "imr_resources/imr_upstox_tokens.json")
TRADE_LOG = os.path.join(BASE_DIR, "imr_resources/imr_trade_log.csv")

# Google Sheets Config
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SERVICE_ACCOUNT_FILE = os.path.join(BASE_DIR, "imr_resources/orb_gsheet_keys.json")
SAMPLESPREADSHEETID  = '1zGorDnhEIUEqh8I4mRBpYUwFPrOC8c_KRf9qrh-Eemo'

# Initialize Sheet Service API Connection
try:
    _gcreds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    service   = build('sheets', 'v4', credentials=_gcreds)
    sheet_api = service.spreadsheets()
except Exception as _e:
    print(f"⚠️ Google Sheet Connection Error: {_e}")
    sheet_api = None

# Load API Variables
try:
    with open(CRED_FILE) as f:
        _creds = json.load(f)
    API_KEY             = _creds["api_key"]
    API_SECRET          = _creds["api_secret"]
    REDIRECT_URI        = _creds["redirect_uri"]
    DISCORD_WEBHOOK_URL = _creds.get("discord_webhook_url", "")
    TOTP                = _creds.get("totp_secret")
    MOBILE_NUM          = _creds.get("mobile_number")
    PIN                 = _creds.get("pin")
    SCREENER_URL        = _creds.get("screener_url", "")
except Exception as e:
    print(f"❌ Error loading creds: {e}")
    exit()

if not os.path.exists(TRADE_LOG):
    pd.DataFrame(columns=["Time","Symbol","Type","Strike","CpType","Qty","Entry","SL","Target","Exit","Capital_Req","PnL","Status","Charges","Net_PnL"]).to_csv(TRADE_LOG, index=False)

# ==========================================
# ALERTING & NOTIFICATIONS (DISCORD EXCLUSIVE)
# ==========================================
def send_discord_notification(message):
    if DISCORD_WEBHOOK_URL:
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=5)
        except Exception as e: 
            print(f"❌ Discord Error: {e}")

# ==========================================
# UPSTOX ACCESS & MARKET FUNCTIONS
# ==========================================
def load_tokens():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f: return json.load(f)
    return None

def get_new_tokens_with_code():
    params = {"client_id": API_KEY, "response_type": "code", "redirect_uri": REDIRECT_URI, "scope": "all"}
    login_url = "https://api.upstox.com/v2/login/authorization/dialog?" + urlencode(params)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_page()
        page.goto(login_url)
        page.fill("#mobileNum", MOBILE_NUM)
        page.click("button:has-text('Get OTP')")
        page.wait_for_selector("#otpNum")
        page.fill("#otpNum", pyotp.TOTP(TOTP).now())
        page.click("button:has-text('Continue')")
        page.wait_for_selector("input[type='password']")
        page.fill("input[type='password']", PIN)
        page.click("button:has-text('Continue')")
        page.wait_for_url(lambda url: REDIRECT_URI in url, timeout=30000)
        code = parse_qs(urlparse(page.url).query).get('code', [None])[0]
        browser.close()

    resp = requests.post("https://api.upstox.com/v2/login/authorization/token", 
                         data={"code": code, "client_id": API_KEY, "client_secret": API_SECRET, "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code"})
    tokens = resp.json()
    with open(TOKEN_FILE, "w") as f: json.dump(tokens, f)
    send_discord_notification("✅ **Logged in to Upstox API Successfully**")
    return tokens

def get_access_token():
    tokens = load_tokens() or get_new_tokens_with_code()
    access_token = tokens.get("access_token")
    if requests.get("https://api.upstox.com/v2/user/profile", headers={"Authorization": f"Bearer {access_token}"}).status_code == 401:
        access_token = get_new_tokens_with_code().get("access_token")
    return access_token

def fetch_ltp(access_token, instrument_key):
    try:
        url = "https://api.upstox.com/v2/market-quote/ltp"
        resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"}, params={"instrument_key": instrument_key}, timeout=5)
        if resp.status_code == 200:
            return resp.json().get("data", {}).get(instrument_key, {}).get("last_price")
    except:
        pass
    return None

def get_historical_and_ltp_data(access_token, instrument_key):
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    quote_url = "https://api.upstox.com/v2/market-quote/quotes"
    to_date = datetime.now(IST).strftime("%Y-%m-%d")
    from_date = (datetime.now(IST) - timedelta(days=400)).strftime("%Y-%m-%d")
    history_url = f"https://api.upstox.com/v2/historical-candle/{instrument_key}/day/{to_date}/{from_date}"
    
    try:
        q_resp = requests.get(quote_url, headers=headers, params={"instrument_key": instrument_key}, timeout=5)
        if q_resp.status_code != 200: return None, None, None
        q_data = q_resp.json().get("data", {}).get(instrument_key, {})
        ltp = q_data.get("last_price")
        volume = q_data.get("volume")
        
        h_resp = requests.get(history_url, headers=headers, timeout=5)
        if h_resp.status_code != 200: return ltp, volume, None
        
        candles = h_resp.json().get("data", {}).get("candles", [])
        if not candles: return ltp, volume, None
        
        df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi'])
        df = df.iloc[::-1].reset_index(drop=True)
        df['200_EMA'] = df['close'].ewm(span=200, adjust=False).mean()
        ema_200 = round(df['200_EMA'].iloc[-1], 2)
        
        return ltp, volume, ema_200
    except Exception as e:
        print(f"❌ Error getting metrics for {instrument_key}: {e}")
        return None, None, None

# ==========================================
# TRANSACTION LOGGING ENGINE
# ==========================================
def log_trade(symbol_name, typ, strike, cp_type, qty, entry, sl, target, exit_p, capital_req, status):
    try:
        entry_val, exit_val, qty_val = float(entry), float(exit_p), int(qty)
        pnl = round((exit_val - entry_val) * qty_val, 2)
        now_ist = datetime.now(IST)
        datetime_combined = "'" + now_ist.strftime('%d-%m-%Y %H:%M')

        brokerage_amt    = 40.0
        turnover         = (entry_val + exit_val) * qty_val
        exchange_charges = turnover * 0.00035
        gst              = (brokerage_amt + exchange_charges) * 0.18
        stt              = exit_val * qty_val * 0.0015
        stamp_duty       = entry_val * qty_val * 0.00003
        total_fees       = round(brokerage_amt + exchange_charges + gst + stt + stamp_duty, 2)
        net_pnl          = round(pnl - total_fees, 2)

        csv_row = [datetime_combined, symbol_name, typ, strike, cp_type, qty_val, entry_val, sl, target, exit_val, capital_req, pnl, status, total_fees, net_pnl]
        pd.DataFrame([csv_row]).to_csv(TRADE_LOG, mode='a', header=False, index=False)

        if sheet_api is not None:
            body = {'values': [csv_row]}
            sheet_api.values().append(
                spreadsheetId=SAMPLESPREADSHEETID, range='imrOrb!A1',
                valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS", body=body
            ).execute()
    except Exception as e:
        print(f"❌ Error logging trade: {e}")

# ==========================================
# ACTIVE PORTFOLIO MONITORING & TRACKING
# ==========================================
def monitor_and_manage_active_trades(access_token):
    if sheet_api is None: return

    try:
        result = sheet_api.values().get(spreadsheetId=SAMPLESPREADSHEETID, range='imrOrb!A:O').execute()
        rows = result.get('values', [])
        if len(rows) <= 1: return 
        
        for idx, row in enumerate(rows[1:], start=2):
            if len(row) < 13: continue
            if row[12] != "ACTIVE": continue
            
            symbol = row[1]
            entry_price = float(row[6])
            
            current_ltp = fetch_ltp(access_token, f"NSE_EQ:{symbol.upper()}")
            if not current_ltp: continue
            
            pnl_pct = ((current_ltp - entry_price) / entry_price) * 100
            exit_triggered = False
            reason = ""
            
            if pnl_pct >= 25.0:
                exit_triggered = True
                reason = "Target Hit (+25%)"
            elif pnl_pct <= -14.0:
                exit_triggered = True
                reason = "SL Hit (-14%)"
                
            if exit_triggered:
                alert_msg = (
                    f"🚨 **SWING PAPER TRADE EXIT** 🚨\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"**Stock:** {symbol.upper()}\n"
                    f"**Exit Reason:** {reason}\n"
                    f"**Entry Price:** ₹{entry_price} | **Exit Price:** ₹{current_ltp}\n"
                    f"**Trade Return:** {pnl_pct:.2f}%\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
                send_discord_notification(alert_msg)
                
                sheet_api.values().update(
                    spreadsheetId=SAMPLESPREADSHEETID, range=f'imrOrb!M{idx}',
                    valueInputOption="USER_ENTERED", body={"values": [[reason.upper()]]}
                ).execute()
                
                sheet_api.values().update(
                    spreadsheetId=SAMPLESPREADSHEETID, range=f'imrOrb!J{idx}',
                    valueInputOption="USER_ENTERED", body={"values": [[current_ltp]]}
                ).execute()
                
                try:
                    df_local = pd.read_csv(TRADE_LOG)
                    df_local.loc[(df_local['Symbol'] == symbol) & (df_local['Status'] == 'ACTIVE'), 'Exit'] = current_ltp
                    df_local.loc[(df_local['Symbol'] == symbol) & (df_local['Status'] == 'ACTIVE'), 'Status'] = reason.upper()
                    df_local.to_csv(TRADE_LOG, index=False)
                except Exception as csv_err:
                    print(f"❌ Local CSV sync error: {csv_err}")
                    
    except Exception as e:
        print(f"❌ Error monitoring active positions: {e}")

# ==========================================
# SCREENER PROCESSING ENGINE
# ==========================================
def fetch_screener_stocks():
    if not SCREENER_URL: return []
    try:
        df = pd.read_csv(SCREENER_URL)
        symbol_col = [col for col in df.columns if 'symbol' in col.lower() or 'ticker' in col.lower()]
        if symbol_col: return df[symbol_col[0]].dropna().unique().tolist()
        return df.iloc[:, 0].dropna().unique().tolist()
    except:
        return []

def get_active_paper_trades():
    if sheet_api is None: return []
    try:
        result = sheet_api.values().get(spreadsheetId=SAMPLESPREADSHEETID, range='imrOrb!A:O').execute()
        rows = result.get('values', [])
        if len(rows) <= 1: return []
        return [row[1] for row in rows[1:] if len(row) > 12 and row[12] == "ACTIVE"]
    except:
        return []

def process_swing_strategy(access_token):
    symbols = fetch_screener_stocks()
    if not symbols: return

    active_trades = get_active_paper_trades()
    
    for ticker in symbols:
        inst_key = f"NSE_EQ:{ticker.upper()}"
        ltp, volume, ema_200 = get_historical_and_ltp_data(access_token, inst_key)
        
        if not ltp or not ema_200: continue
            
        if ltp < ema_200:
            pct_below = round(((ema_200 - ltp) / ema_200) * 100, 2)
            
            if pct_below <= 14.0 and ticker.upper() not in active_trades:
                sl_price = round(ltp * 0.86, 2)     # Strict 14% Stop Loss
                target_price = round(ltp * 1.25, 2) # Strict 25% Target Take-Profit
                qty_placeholder = 100
                capital_needed = round(ltp * qty_placeholder, 2)
                
                alert_msg = (
                    f"📈 **SWING RADAR ENTRY** 📈\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"**Stock:** {ticker.upper()}\n"
                    f"**LTP:** ₹{ltp} | **Vol:** {volume}\n"
                    f"**200 EMA:** ₹{ema_200} ({pct_below}% Below)\n"
                    f"**SL (14%):** ₹{sl_price}\n"
                    f"**Target (25%):** ₹{target_price}\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
                send_discord_notification(alert_msg)
                
                log_trade(
                    symbol_name=ticker.upper(), typ="SWING_BUY", strike="-", cp_type="-",
                    qty=qty_placeholder, entry=ltp, sl=sl_price, target=target_price,
                    exit_p=0, capital_req=capital_needed, status="ACTIVE"
                )
        time.sleep(0.5)

# ==========================================
# MAIN EXECUTION CORE
# ==========================================
def main():
    access_token = get_access_token()
    if not access_token:
        print("❌ Failed to get access token. Exiting.")
        return

    ti = datetime.now(IST).strftime('%d-%m-%Y %H:%M')
    print("🚀 IMR Momentum Master v2 Initialized.")
    
    send_discord_notification(
        f"==================================\n"
        f"⏰ {ti}\n"
        f"🚀 **Welcome to IMR Momentum Algo**\n"
        f"*Multi-filter swing engine active.*\n"
        f"*Starting Algo-Bot... Tighten your seat belts 💺 !*\n"
        f"=================================="
    )
    
    # Wait for 9:20 AM for first 5-min candle to close
    while datetime.now(IST).time() < dt_time(9, 20):
        print(f"⏳ Waiting for 09:20 AM... {datetime.now(IST).time().strftime('%H:%M:%S')}", end='\r')
        time.sleep(5)
        
    print("\n⏰ 09:20 AM Market Monitor Active. Evaluating positions...")
    
    # Step 1: Check existing paper positions for Target / SL exits
    monitor_and_manage_active_trades(access_token)
    
    # Step 2: Query screener data to scan for fresh entries
    process_swing_strategy(access_token)

if __name__ == "__main__":
    main()