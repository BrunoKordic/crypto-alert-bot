import eventlet
eventlet.monkey_patch()

import json
import websocket
import requests
import logging
from flask import Flask, render_template, request
from flask_socketio import SocketIO
import threading

# --- Basic Flask App Setup ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-very-secret-key!'
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

# --- Configuration & Bot Logic ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- IMPORTANT: PASTE YOUR CREDENTIALS HERE ---
TELEGRAM_TOKEN = "8198854299:AAHs7WTRyqfk_EtvEs98YeY0b8vbf44ptOs"
TELEGRAM_CHAT_ID = "1969994554"

# --- Global State Management ---
ACTIVE_TASKS = {}
lock = threading.Lock()
BINANCE_SYMBOLS = []
SYMBOLS_LOADED = threading.Event()

# --- Binance API Functions ---
def get_binance_usdt_symbols():
    """Fetches all USDT trading pairs from Binance."""
    url = "https://api.binance.com/api/v3/exchangeInfo"
    logging.info("Attempting to fetch symbols from Binance...")
    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        data = response.json()
        
        symbols = [
            s['symbol'] for s in data.get('symbols', []) 
            if s.get('status') == 'TRADING' 
            and s.get('quoteAsset') == 'USDT' 
            and 'SPOT' in s.get('permissions', [])
        ]
        symbols.sort()
        
        if not symbols:
            raise ValueError("Filtering returned no symbols.")
            
        logging.info(f"Successfully fetched {len(symbols)} USDT trading pairs.")
        return symbols
    except Exception as e:
        logging.error(f"Could not fetch/filter symbols from Binance: {e}. Using fallback list.")
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "BNBUSDT", "AVAXUSDT"]

# --- Telegram & Alerting Functions ---
def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or "YOUR_TELEGRAM" in TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logging.error(f"An error occurred while sending Telegram message: {e}")

# --- Binance Monitoring Logic ---
def monitor_coin(symbol, slot_id, thresholds):
    state = {
        "slot_id": slot_id, "symbol": symbol, "thresholds": thresholds,
        "base_price": 0, "current_price": 0, "percentage_change": 0,
        "triggered_alerts": set()
    }
    with lock:
        if slot_id in ACTIVE_TASKS:
            ACTIVE_TASKS[slot_id]['state'] = state
        else: return

    def check_for_alert(current_price):
        if state["base_price"] == 0: return
        state['percentage_change'] = ((current_price - state["base_price"]) / state["base_price"]) * 100
        update_data = {'current_price': state['current_price'], 'base_price': state['base_price'], 'percentage_change': state['percentage_change']}
        socketio.emit('status_update', {'slot_id': slot_id, 'data': update_data})
        for threshold in state["thresholds"]:
            if threshold in state["triggered_alerts"]: continue
            if (threshold > 0 and state["percentage_change"] >= threshold) or (threshold < 0 and state["percentage_change"] <= threshold):
                direction = "up" if state['percentage_change'] > 0 else "down"
                icon = "📈" if direction == "up" else "📉"
                tg_message = f"{icon} *{symbol} Alert* {icon}\n\nPrice is {direction} *{state['percentage_change']:+.2f}%*.\n\n*Current Price:* `${current_price:,.4f}`\n*Base Price:* `${state['base_price']:,.4f}`"
                send_telegram_message(tg_message)
                log_message = f"ALERT: {direction} {state['percentage_change']:.2f}% (Threshold: {threshold}%)"
                socketio.emit('log_message', {'slot_id': slot_id, 'message': log_message, 'type': 'danger'})
                state["base_price"] = current_price
                state["triggered_alerts"].clear()
                reset_message_tg = f"✅ *{symbol}* base price reset to `${current_price:,.4f}`."
                reset_message_ui = f"Base price reset to ${current_price:,.4f}."
                send_telegram_message(reset_message_tg)
                socketio.emit('log_message', {'slot_id': slot_id, 'message': reset_message_ui, 'type': 'info'})
                break

    def on_message(ws, message):
        data = json.loads(message)
        current_price = float(data['p'])
        state["current_price"] = current_price
        if state["base_price"] == 0:
            state["base_price"] = current_price
            send_telegram_message(f"✅ Started monitoring *{symbol}*.\nBase Price: `${current_price:,.4f}`")
            socketio.emit('log_message', {'slot_id': slot_id, 'message': f'Base price set to ${current_price:,.4f}', 'type': 'success'})
        check_for_alert(current_price)

    def on_error(ws, error):
        socketio.emit('log_message', {'slot_id': slot_id, 'message': f"Connection Error: {error}", 'type': 'error'})

    def on_close(ws, close_status_code, close_msg):
        socketio.emit('task_stopped', {'slot_id': slot_id})
        with lock:
            if slot_id in ACTIVE_TASKS: del ACTIVE_TASKS[slot_id]

    ws_url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@trade"
    ws = websocket.WebSocketApp(ws_url, on_message=on_message, on_error=on_error, on_close=on_close)
    with lock:
        if slot_id in ACTIVE_TASKS: ACTIVE_TASKS[slot_id]['ws'] = ws
    ws.run_forever(ping_interval=20, ping_timeout=10)

# --- Flask Routes and SocketIO Events ---
@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    logging.info(f'Client connected: {request.sid}')
    # If symbols are already loaded, send them immediately.
    if SYMBOLS_LOADED.is_set():
        socketio.emit('symbol_list', {'symbols': BINANCE_SYMBOLS}, room=request.sid)

@socketio.on('request_symbol_list')
def handle_request_symbol_list():
    # Allows the client to ask for the list again if it connected before they were loaded.
    if SYMBOLS_LOADED.is_set():
        socketio.emit('symbol_list', {'symbols': BINANCE_SYMBOLS}, room=request.sid)

@socketio.on('start_monitoring')
def handle_start_monitoring(data):
    slot_id, symbol, thresholds = data['slot_id'], data['symbol'].upper(), data['thresholds']
    with lock:
        if slot_id in ACTIVE_TASKS: return
    task_thread = socketio.start_background_task(monitor_coin, symbol, slot_id, thresholds)
    with lock:
        ACTIVE_TASKS[slot_id] = {'thread': task_thread, 'ws': None}
    socketio.emit('task_started', {'slot_id': slot_id})

@socketio.on('stop_monitoring')
def handle_stop_monitoring(data):
    slot_id = data['slot_id']
    with lock:
        if slot_id in ACTIVE_TASKS and ACTIVE_TASKS[slot_id].get('ws'):
            ACTIVE_TASKS[slot_id]['ws'].close()

@socketio.on('disconnect')
def handle_disconnect():
    logging.info(f'Client disconnected: {request.sid}')

# --- Main Execution ---
def initial_setup():
    """Background task to fetch symbols and notify all clients."""
    global BINANCE_SYMBOLS
    logging.info("Starting initial setup: fetching Binance symbols.")
    BINANCE_SYMBOLS = get_binance_usdt_symbols()
    SYMBOLS_LOADED.set() # Mark symbols as loaded
    logging.info("Initial setup finished: Binance symbols are loaded and ready.")
    # Notify any already connected clients
    socketio.emit('symbol_list', {'symbols': BINANCE_SYMBOLS})

if __name__ == '__main__':
    # **FINAL FIX:** Use the official socketio method to start the background task.
    socketio.start_background_task(initial_setup)
    logging.info("Starting Flask-SocketIO server...")
    socketio.run(app, host='0.0.0.0', port=5000)
