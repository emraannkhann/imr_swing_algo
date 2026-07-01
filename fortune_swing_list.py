import os
import json
import time
import requests
import pandas as pd
import pytz
import yfinance as yf
from bs4 import BeautifulSoup
from datetime import datetime, time as dt_time, timedelta
from googleapiclient.discovery import build
from google.oauth2 import service_account
from playwright.sync_api import sync_playwright

# ==========================================
# CONSTANTS & CONFIGURATION MATCHES
# ==========================================
IST = pytz.timezone('Asia/Kolkata')

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
CRED_FILE = os.path.join(BASE_DIR, "imr_resources/imr_upstox_creds.json")
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

# Load Configs
try:
    with open(CRED_FILE) as f:
        _creds = json.load(f)
    DISCORD_WEBHOOK_URL = _creds.get("discord_webhook_url", "")
    print(f"✅ Loaded Discord Webhook URL: {DISCORD_WEBHOOK_URL[:30]}...")
    SCREENER_URL        = _creds.get("screener_url", "")
except Exception as e:
    print(f"❌ Error loading creds: {e}")
    exit()

if not os.path.exists(TRADE_LOG):
    pd.DataFrame(columns=["Time","Stock","Qty","Entry","SL","Exit","Target","Capital Req","PnL","NetPnL"]).to_csv(TRADE_LOG, index=False)

# ==========================================
# HEADLESS CLOUD SCREENER PARSER
# ==========================================
def fetch_screener_stocks():
    """
    Optimized headless configuration designed to prevent cloud blocking
    and eliminate networkidle timeout traps on AWS instances.
    """
    if not SCREENER_URL:
        print("⚠️ Screener URL missing inside cred file.")
        return []

    symbols = []
    print(f"🌐 AWS Linux Environment: Launching Protected Headless Chromium...")
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",               # CRITICAL FOR AWS LINUX SERVER
                    "--blink-settings=imagesEnabled=false"
                ]
            )
            # Configure viewport and localized device masking properties
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="en-IN",
                timezone_id="Asia/Kolkata"
            )
            
            page = context.new_page()
            
            # FIX 1: Overriding default navigation timeouts safely
            page.set_default_navigation_timeout(45000)
            
            # FIX 2: Swap "networkidle" for "domcontentloaded" (Bypasses analytics loops)
            print(f"📡 Navigating to layout target...")
            page.goto(SCREENER_URL, wait_until="domcontentloaded")
            
            # FIX 3: Give static components a brief window to settle without hanging indefinitely
            page.wait_for_timeout(3000) 
            
            # Read content from live DOM engine 
            html_content = page.content()
            browser.close()
            
        # Parse content matching exact table mappings
        soup = BeautifulSoup(html_content, 'html.parser')
        table = soup.find('table', {'class': 'data-table'}) or soup.find('table')
        
        if not table:
            print("⚠️ Data table missing. The server might have triggered a Cloudflare captcha block.")
            return []
            
        for row in table.find_all('tr'):
            for link in row.find_all('a', href=True):
                href = link['href']
                if "/company/" in href:
                    parts = href.split('/')
                    try:
                        idx = parts.index("company")
                        ticker = parts[idx + 1].upper()
                        if ticker not in symbols:
                            symbols.append(ticker)
                    except ValueError:
                        continue
                        
        print(f"📊 Successfully extracted {len(symbols)} tickers.")
        return symbols
        
    except Exception as e:
        print(f"❌ Playwright execution exception: {e}")
        return []
# ==========================================
# ALERTING & NOTIFICATIONS
# ==========================================
def send_discord_notification(message):
    if DISCORD_WEBHOOK_URL:
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=5)
        except Exception as e: 
            print(f"❌ Discord Error: {e}")

# ==========================================
# TRANSACTION LOGGING ENGINE (A TO J MATCH)
# ==========================================
def log_trade(symbol_name, entry, volume):
    try:
        entry_val = float(entry)
        now_ist = datetime.now(IST)
        datetime_combined = "'" + now_ist.strftime('%d-%m-%Y %H:%M')
        
        sl_price = round(entry_val * 0.86, 2)     # Strict 14% Stop Loss
        target_price = round(entry_val * 1.25, 2) # Strict 25% Profit Target
        qty_placeholder = 100                     
        capital_needed = round(entry_val * qty_placeholder, 2)

        csv_row = [
            datetime_combined,     # Column A: Time
            symbol_name.upper(),   # Column B: Stock
            qty_placeholder,       # Column C: Qty
            entry_val,             # Column D: Entry
            sl_price,              # Column E: SL
            0,                     # Column F: Exit
            target_price,          # Column G: Target
            capital_needed,        # Column H: Capital Req
            0,                     # Column I: PnL
            0                      # Column J: NetPnL
        ]
        
        pd.DataFrame([csv_row]).to_csv(TRADE_LOG, mode='a', header=False, index=False)

        if sheet_api is not None:
            body = {'values': [csv_row]}
            sheet_api.values().append(
                spreadsheetId=SAMPLESPREADSHEETID, range='swingTrds!A1',
                valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS", body=body
            ).execute()
            print(f"📊 Google Sheets successfully updated for {symbol_name}")
            
    except Exception as e:
        print(f"❌ Error logging trade columns to Sheet: {e}")

# ==========================================
# YAHOO FINANCE DATA BACKEND
# ==========================================
def get_historical_and_ltp_via_yfinance(ticker):
    try:
        yf_ticker = f"{ticker.upper()}.NS"
        stock = yf.Ticker(yf_ticker)
        df = stock.history(period="2y", interval="1d")
        if df.empty or len(df) < 200: return None, None, None
        
        ltp = round(df['Close'].iloc[-1], 2)
        volume = int(df['Volume'].iloc[-1])
        
        df['200_EMA'] = df['Close'].ewm(span=200, adjust=False).mean()
        ema_200 = round(df['200_EMA'].iloc[-1], 2)
        
        return ltp, volume, ema_200
    except:
        return None, None, None

# ==========================================
# PORTFOLIO MONITORING & RECONCILIATION
# ==========================================
def monitor_and_manage_active_trades():
    if sheet_api is None: return

    try:
        result = sheet_api.values().get(spreadsheetId=SAMPLESPREADSHEETID, range='swingTrds!A:J').execute()
        rows = result.get('values', [])
        if len(rows) <= 1: return 
        
        for idx, row in enumerate(rows[1:], start=2):
            while len(row) < 10: row.append("")
                
            symbol = row[1]
            qty = int(row[2]) if str(row[2]).isdigit() else 100
            entry_price = float(row[3]) if row[3] else 0.0
            exit_price_current = str(row[5]).strip()
            
            if entry_price == 0.0: continue
            if exit_price_current and exit_price_current != "0" and exit_price_current != "0.0": continue
            
            current_ltp, _, _ = get_historical_and_ltp_via_yfinance(symbol)
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
                gross_pnl = round((current_ltp - entry_price) * qty, 2)
                
                brokerage = 40.0
                turnover = (entry_price + current_ltp) * qty
                exchange_charges = turnover * 0.00035
                gst = (brokerage + exchange_charges) * 0.18
                stt = current_ltp * qty * 0.0015
                stamp_duty = entry_price * qty * 0.00003
                total_fees = round(brokerage + exchange_charges + gst + stt + stamp_duty, 2)
                net_pnl_val = round(gross_pnl - total_fees, 2)
                
                alert_msg = (
                    f"🚨 **SWING PAPER TRADE EXIT** 🚨\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"**Stock:** {symbol.upper()}\n"
                    f"**Exit Reason:** {reason}\n"
                    f"**Entry Price:** ₹{entry_price} | **Exit Price:** ₹{current_ltp}\n"
                    f"**Gross Return:** ₹{gross_pnl:,} ({pnl_pct:.2f}%)\n"
                    f"**Net PnL (incl. charges):** ₹{net_pnl_val:,}\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
                send_discord_notification(alert_msg)
                
                sheet_api.values().update(spreadsheetId=SAMPLESPREADSHEETID, range=f'swingTrds!F{idx}', valueInputOption="USER_ENTERED", body={"values": [[current_ltp]]}).execute()
                sheet_api.values().update(spreadsheetId=SAMPLESPREADSHEETID, range=f'swingTrds!I{idx}', valueInputOption="USER_ENTERED", body={"values": [[gross_pnl]]}).execute()
                sheet_api.values().update(spreadsheetId=SAMPLESPREADSHEETID, range=f'swingTrds!J{idx}', valueInputOption="USER_ENTERED", body={"values": [[net_pnl_val]]}).execute()
                    
    except Exception as e:
        print(f"❌ Error monitoring active positions: {e}")

def get_active_paper_trades():
    if sheet_api is None: return []
    try:
        result = sheet_api.values().get(spreadsheetId=SAMPLESPREADSHEETID, range='swingTrds!A:F').execute()
        rows = result.get('values', [])
        if len(rows) <= 1: return []
        
        active_symbols = []
        for row in rows[1:]:
            if len(row) < 6: 
                active_symbols.append(row[1].upper())
                continue
            exit_val = str(row[5]).strip()
            if not exit_val or exit_val == "0" or exit_val == "0.0":
                active_symbols.append(row[1].upper())
        return active_symbols
    except:
        return []

def process_swing_strategy():
    symbols = fetch_screener_stocks()
    print(f"{'=*18'}\n🔍 Processing {len(symbols)} symbols from Screener for Swing Radar...")
    if not symbols: return

    active_positions = get_active_paper_trades()
    print(f"📌 Currently Active Paper Trades: {len(active_positions)}")
    for ticker in symbols:
        ticker_clean = ticker.strip().upper()
        print(f"{'=*18'}\n⏳ Evaluating {ticker_clean} for Swing Radar...")
        ltp, volume, ema_200 = get_historical_and_ltp_via_yfinance(ticker_clean)
        print(f"{'=*18'}\n📊 {ticker_clean} | LTP: {ltp} | Volume: {volume} | 200 EMA: {ema_200}")
        if not ltp or not ema_200: continue
            
        if ltp < ema_200:
            pct_below = round(((ema_200 - ltp) / ema_200) * 100, 2)
            
            if pct_below <= 14.0 and ticker_clean not in active_positions:
                sl_calc = round(ltp * 0.86, 2)
                tgt_calc = round(ltp * 1.25, 2)
                
                alert_msg = (
                    f"📈 **SWING RADAR ENTRY FOUND** 📈\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"**Stock:** {ticker_clean}\n"
                    f"**Current LTP:** ₹{ltp}\n"
                    f"**Day Volume:** {volume:,}\n"
                    f"**200 EMA Value:** ₹{ema_200} ({pct_below}% Below EMA)\n"
                    f"**Calculated SL (14%):** ₹{sl_calc}\n"
                    f"**Calculated Target (25%):** ₹{tgt_calc}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━"
                )
                send_discord_notification(alert_msg)
                log_trade(symbol_name=ticker_clean, entry=ltp, volume=volume)
                
        time.sleep(1)

# ==========================================
# MAIN EXECUTION CORE
# ==========================================
def main():
    ti = datetime.now(IST).strftime('%d-%m-%Y %H:%M')
    print("🚀 Swing BOT Execution Hook Active.")
    
    send_discord_notification(
        f"==================================\n"
        f"⏰ {ti}\n"
        f"🚀 **Swing BOT Active**\n"
        f"=================================="
    )
    
    while datetime.now(IST).time() < dt_time(9, 20):
        print(f"⏳ Waiting for 09:20 AM... {datetime.now(IST).time().strftime('%H:%M:%S')}", end='\r')
        time.sleep(5)
        
    print("\n⏰ 09:20 AM Core Strategy Loop Executed. Checking Active Positions...")
    #monitor_and_manage_active_trades()
    process_swing_strategy()

if __name__ == "__main__":
    main()