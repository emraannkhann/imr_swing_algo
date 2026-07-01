import os
import json
import time
import requests
import pandas as pd
import numpy as np
import pytz
import yfinance as yf
from bs4 import BeautifulSoup
from datetime import datetime, time as dt_time, timedelta
from googleapiclient.discovery import build
from google.oauth2 import service_account
from playwright.sync_api import sync_playwright
import socket
from google.auth.transport.requests import Request as GoogleAuthRequest

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
socket.setdefaulttimeout(30)
try:
    
    _gcreds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    
    # Clean, standard initialization with NO arguments missing
    service = build('sheets', 'v4', credentials=_gcreds)
    sheet_api = service.spreadsheets()
    
    print("✅ Google Sheets API Client built successfully with global socket timeout.")
except Exception as _e:
    print(f"⚠️ Google Sheet Connection Error: {_e}")
    sheet_api = None
try:
    with open(CRED_FILE) as f:
        _creds = json.load(f)
    DISCORD_WEBHOOK_URL = _creds.get("discord_webhook_url", "")
    SCREENER_URL        = _creds.get("screener_url", "")
except Exception as e:
    print(f"❌ Error loading creds: {e}")
    exit()

if not os.path.exists(TRADE_LOG):
    pd.DataFrame(columns=["Time","Stock","Qty","Entry","SL","Exit","Target","Capital Req","PnL","NetPnL"]).to_csv(TRADE_LOG, index=False)

# ==========================================
# VECTORIZED QUANT INDICATOR ENGINE
# ==========================================
def calculate_quant_metrics(df):
    """
    Computes technical metrics using vectorized pandas operations 
    with strict index matching to prevent dimension alignment crashes.
    """
    if len(df) < 250:
        return None

    # 1. Moving Averages
    df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['EMA200'] = df['Close'].ewm(span=200, adjust=False).mean()

    # 2. RSI (14) Calculation
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / (loss + 1e-10)
    df['RSI'] = 100 - (100 / (1 + rs))

    # 3. ATR (14) & ADX (14) Calculations
    df['H-L'] = df['High'] - df['Low']
    df['H-PC'] = abs(df['High'] - df['Close'].shift(1))
    df['L-PC'] = abs(df['Low'] - df['Close'].shift(1))
    df['TR'] = df[['H-L', 'H-PC', 'L-PC']].max(axis=1)
    df['ATR'] = df['TR'].ewm(alpha=1/14, adjust=False).mean()

    # Directional Movement for ADX
    up_move = df['High'].diff()
    down_move = df['Low'].shift(1) - df['Low']
    
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    
    # CRITICAL FIX: Explicitly enforce index=df.index to maintain alignment shape
    plus_di = 100 * (pd.Series(plus_dm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / (df['ATR'] + 1e-10))
    minus_di = 100 * (pd.Series(minus_dm, index=df.index).ewm(alpha=1/14, adjust=False).mean() / (df['ATR'] + 1e-10))
    
    di_sum = plus_di + minus_di
    di_diff = abs(plus_di - minus_di)
    dx = 100 * (di_diff / (di_sum + 1e-10))
    
    # CRITICAL FIX: Explicitly enforce index=df.index here as well
    df['ADX'] = pd.Series(dx, index=df.index).ewm(alpha=1/14, adjust=False).mean()

    # 4. Volume & Benchmark Statistics
    df['AvgVolume20'] = df['Volume'].rolling(window=20).mean()
    df['52W_High'] = df['Close'].rolling(window=250).max()

    return df.iloc[-1]

def score_stock_institutional(metrics):
    """
    Scores candidates out of 100 based on core multi-factor momentum parameters.
    """
    score = 0
    close = metrics['Close']
    
    # Factor 1: Alignment Matrix (Max 30 Pts)
    if close > metrics['EMA20'] > metrics['EMA50'] > metrics['EMA200']:
        score += 30
    elif close > metrics['EMA50'] > metrics['EMA200']:
        score += 15

    # Factor 2: RSI Squeeze Sweet-Spot (Max 25 Pts)
    if 55 <= metrics['RSI'] <= 70:
        score += 25
    elif 50 <= metrics['RSI'] < 55:
        score += 15

    # Factor 3: ADX Trend Velocity Strength (Max 25 Pts)
    if metrics['ADX'] > 25:
        score += 25
    elif metrics['ADX'] > 20:
        score += 10

    # Factor 4: Institutional Volume Expansion Burst (Max 20 Pts)
    if metrics['Volume'] >= (metrics['AvgVolume20'] * 2):
        score += 20
    elif metrics['Volume'] >= (metrics['AvgVolume20'] * 1.5):
        score += 10

    return score

# ==========================================
# HEADLESS CLOUD SCREENER PARSER
# ==========================================
def fetch_screener_stocks():
    if not SCREENER_URL:
        return []
    symbols = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", locale="en-IN", timezone_id="Asia/Kolkata")
            page = context.new_page()
            page.set_default_navigation_timeout(45000)
            page.goto(SCREENER_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(3000) 
            html_content = page.content()
            browser.close()
            
        soup = BeautifulSoup(html_content, 'html.parser')
        table = soup.find('table', {'class': 'data-table'}) or soup.find('table')
        if not table: return []
            
        for row in table.find_all('tr'):
            for link in row.find_all('a', href=True):
                href = link['href']
                if "/company/" in href:
                    parts = href.split('/')
                    try:
                        idx = parts.index("company")
                        ticker = parts[idx + 1].upper()
                        if ticker not in symbols: symbols.append(ticker)
                    except ValueError: continue
                    print(f"✅ Extracted symbol: {ticker}")
        return symbols
    except Exception as e:
        print(f"❌ Playwright extraction exception: {e}")
        return []

# ==========================================
# NOTIFICATIONS & ACTIVE TRACKERS
# ==========================================
def send_discord_notification(message):
    if not DISCORD_WEBHOOK_URL: return
    try: requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=15)
    except Exception as e: print(f"❌ Discord Error: {e}")

def get_active_paper_trades():
    if sheet_api is None: return []
    try:
        result = sheet_api.values().get(spreadsheetId=SAMPLESPREADSHEETID, range='swingTrds!A:F').execute()
        rows = result.get('values', [])
        if len(rows) <= 1: return []
        return [row[1].upper() for row in rows[1:] if len(row) > 5 and (not row[5] or row[5] == "0" or row[5] == "0.0")]
    except: return []

# def log_trade(symbol_name, entry, score):
#     try:
#         entry_val = float(entry)
#         now_ist = datetime.now(IST)
#         datetime_combined = now_ist.strftime('%d-%m-%Y %H:%M')
        
#         sl_price = round(entry_val * 0.86, 2)     # Strict 14% Stop Loss
#         target_price = round(entry_val * 1.25, 2) # Strict 25% Profit Target
#         qty_placeholder = 100                     
#         capital_needed = round(entry_val * qty_placeholder, 2)

#         csv_row = [
#             str(datetime_combined),     # Column A: Time
#             str(symbol_name).upper(),   # Column B: Stock
#             int(qty_placeholder),       # Column C: Qty
#             float(entry_val),           # Column D: Entry
#             float(sl_price),            # Column E: SL
#             0,                          # Column F: Exit
#             float(target_price),        # Column G: Target
#             float(capital_needed),      # Column H: Capital Req
#             0,                          # Column I: PnL
#             0                           # Column J: NetPnL
#         ]
        
#         # Backup locally
#         pd.DataFrame([csv_row]).to_csv(TRADE_LOG, mode='a', header=False, index=False)

#         if sheet_api is not None:
#             # 1. Fetch current visible values to find the EXACT next row number
#             result = sheet_api.values().get(
#                 spreadsheetId=SAMPLESPREADSHEETID, 
#                 range='swingTrds!A:B'
#             ).execute()
            
#             rows = result.get('values', [])
#             next_row = len(rows) + 1 # Calculates exact row number (e.g., if rows=1, next is 2)
            
#             # 2. Use target write ('UPDATE') instead of 'APPEND' to force placement
#             target_range = f'swingTrds!A{next_row}:J{next_row}'
#             body = {'values': [csv_row]}
            
#             sheet_api.values().update(
#                 spreadsheetId=SAMPLESPREADSHEETID, 
#                 range=target_range,
#                 valueInputOption="USER_ENTERED", 
#                 body=body
#             ).execute()
            
#             print(f"📊 Google Sheets forced update to row {next_row} for {symbol_name} (Score: {score})")
            
#     except Exception as e:
#         print(f"❌ Error logging trade columns to Sheet: {e}")

def log_trade(symbol_name, entry, score):
    try:
        entry_val = float(entry)
        now_ist = datetime.now(IST)
        datetime_combined = now_ist.strftime('%d-%m-%Y %H:%M')
        
        sl_price = round(entry_val * 0.86, 2)     # Strict 14% Stop Loss
        target_price = round(entry_val * 1.25, 2) # Strict 25% Profit Target
        qty_placeholder = 20                     
        capital_needed = round(entry_val * qty_placeholder, 2)

        csv_row = [
            str(datetime_combined),     # Column A: Time
            str(symbol_name).upper(),   # Column B: Stock
            int(qty_placeholder),       # Column C: Qty
            float(entry_val),           # Column D: Entry
            float(sl_price),            # Column E: SL
            0,                          # Column F: Exit
            float(target_price),        # Column G: Target
            float(capital_needed),      # Column H: Capital Req
            0,                          # Column I: PnL
            0                           # Column J: NetPnL
        ]
        
        # Local backup save
        pd.DataFrame([csv_row]).to_csv(TRADE_LOG, mode='a', header=False, index=False)

        if _gcreds is not None:
            # 1. Force refresh to fetch a clean, valid OAuth2 Access Token
            _gcreds.refresh(GoogleAuthRequest())
            access_token = _gcreds.token
            
            # 2. Build the lightweight direct REST API Endpoint URL
            # We target 'append' with valueInputOption=USER_ENTERED
            url = f"https://sheets.googleapis.com/v4/spreadsheets/{SAMPLESPREADSHEETID}/values/swingTrds!A:A:append"
            
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "range": "swingTrds!A:A",
                "majorDimension": "ROWS",
                "values": [csv_row]
            }
            
            params = {
                "valueInputOption": "USER_ENTERED",
                "insertDataOption": "INSERT_ROWS"
            }
            
            # 3. Fire a raw HTTP POST straight into the Google Sheets endpoint
            print(f"📡 Transmitting micro-payload to Google REST Gateway for {symbol_name}...")
            response = requests.post(url, json=payload, headers=headers, params=params, timeout=20)
            
            if response.status_code == 200:
                print(f"📊 Google Sheets REST update confirmed for {symbol_name} (Score: {score})")
            else:
                print(f"❌ Google Sheets REST Error Code {response.status_code}: {response.text}")
                
    except Exception as e:
        print(f"❌ Critical Exception inside log_trade: {e}")


# ==========================================
# PIPELINE EXECUTION ENGINE
# ==========================================
def process_swing_strategy():
    symbols = fetch_screener_stocks()
    if not symbols: 
        print("⚠️ No symbols found from Screener URL.")
        return

    active_positions = get_active_paper_trades()
    print(f"🔍 Quant Pipeline active: Evaluating {len(symbols)} stocks...")
    print(f"📌 Active positions already in Sheet: {active_positions}")
    
    high_conviction_found = False

    for ticker in symbols:
        try:
            ticker_clean = ticker.strip().upper()
            stock = yf.Ticker(f"{ticker_clean}.NS")
            df = stock.history(period="2y", interval="1d")
            
            latest_metrics = calculate_quant_metrics(df)
            if latest_metrics is None: 
                print(f"⏩ {ticker_clean}: Skipped (Insufficient historical data)")
                continue
            
            # Generate institutional multi-factor scoring calculation
            stock_score = score_stock_institutional(latest_metrics)
            
            # --- LIVE CONSOLE DEBUGGING LOGS ---
            ltp = round(latest_metrics['Close'], 2)
            rsi = round(latest_metrics['RSI'], 1)
            adx = round(latest_metrics['ADX'], 1)
            vol = int(latest_metrics['Volume'])
            avg_vol = int(latest_metrics['AvgVolume20'])
            vol_ratio = round(vol / (avg_vol + 1e-10), 2)
            
            print(f"📊 [SCANNING] {ticker_clean:<10} | Score: {stock_score:<3}/100 | Price: ₹{ltp:<7} | RSI: {rsi:<4} | ADX: {adx:<4} | Vol Ratio: {vol_ratio}x")
            # -----------------------------------
            
            # SELECTION CRITERIA ACCELERATION: Alert/Trade only if conviction score >= 80
            if stock_score >= 80:
                high_conviction_found = True
                if ticker_clean in active_positions: 
                    print(f"ℹ️ {ticker_clean} scored {stock_score}/100 but is already ACTIVE in sheets. Skipping duplicate log.")
                    continue
                
                alert_msg = (
                    f"🔥 **HIGH CONVICTION SWING** 🔥\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"**Stock:** {ticker_clean} | **🎯 SCORE: {stock_score}/100**\n"
                    f"**LTP:** ₹{ltp} | **Vol:** {vol:,} ({vol_ratio}x Accumulation)\n"
                    f"**RSI (14):** {rsi} | **ADX Trend Velocity:** {adx}\n"
                    f"**Calculated Targets:** SL (14%): ₹{round(ltp*0.86,2)} | Tgt (25%): ₹{round(ltp*1.25,2)}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                )
                send_discord_notification(alert_msg)
                log_trade(symbol_name=ticker_clean, entry=ltp, score=stock_score)
                print(f"✅ **LOGGED & NOTIFIED SUCCESSFUL ENTRY FOR {ticker_clean}**")
                
            time.sleep(0.5)
        except Exception as e:
            print(f"⚠️ Exception processing metrics for {ticker}: {e}")

    if not high_conviction_found:
        print("ℹ️ Finished scanning market. No stocks hit the high-conviction threshold (>=80) today.")

def main():
    ti = datetime.now(IST).strftime('%d-%m-%Y %H:%M')
    print("🚀 Institutional Score Architecture Engine Active.")
    send_discord_notification(f"==================================\n⏰ {ti}\n🚀 **FII's Swing Bot Launched**\n==================================")
    
    while datetime.now(IST).time() < dt_time(9, 20):
        time.sleep(5)
        
    process_swing_strategy()

if __name__ == "__main__":
    main()