import os
import logging
import threading
from datetime import datetime, date, timedelta, time
from collections import OrderedDict
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from typing import Optional

from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from longport.openapi import (
    Config, QuoteContext, TradeContext, PushOrderChanged, OrderType, OrderStatus,
    OrderSide, TimeInForceType, TopicType, OutsideRTH
)

# ================== Logging Config ==================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.getLogger('apscheduler').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ================== Enum & Constants ==================
class Action(Enum):
    BUY = "buy"
    SELL = "sell"

# Environment variables
LONGPORT_WEBHOOK_SECRET = os.getenv("LONGPORT_WEBHOOK_SECRET")
CONFIG = Config.from_env()

# Trading parameters
PROFIT_LOSS_RATIO = 0.03  # Take profit/stop loss ratio (+/-3%)
OPEN_COOLDOWN_MINUTES = 10
OPEN_COOLDOWN_SECONDS = OPEN_COOLDOWN_MINUTES * 60
TAKE_PROFIT_RATIO = Decimal(str(1 + PROFIT_LOSS_RATIO))
STOP_LOSS_RATIO = Decimal(str(1 - PROFIT_LOSS_RATIO))
PRICE_PRECISION = Decimal('0.01')
MAX_PROCESSED_ORDERS = 1000
US_MARKET_OPEN_HOUR = int(os.getenv("US_MARKET_OPEN_HOUR", 21))
US_MARKET_OPEN_MINUTE = int(os.getenv("US_MARKET_OPEN_MINUTE", 30))
US_MARKET_CLOSE_HOUR = int(os.getenv("US_MARKET_CLOSE_HOUR", 4))
US_MARKET_CLOSE_MINUTE = int(os.getenv("US_MARKET_CLOSE_MINUTE", 0))

# ================== Global State ==================
g_last_open_time = None
g_position_symbol = None
g_position_price = Decimal('0')
g_position_stop_loss_price = Decimal('0')
g_position_take_profit_price = Decimal('0')
g_stop_order_id = None
g_take_profit_order_id = None
g_position_quantity = Decimal('0')
g_position_lock = threading.Lock()
g_processed_order_ids = OrderedDict()
g_pending_orders = {}  # order_id: (submit_time, symbol)

g_today_trades = []  # [{'action': Action.BUY, 'profit': True/False}]
g_today_date = None
g_today_profit = False

quote_ctx = QuoteContext(CONFIG)
trade_ctx = TradeContext(CONFIG)
app = Flask(__name__)


# ================== Utility Functions ==================
def update_us_stock_trading_hours():
    global US_MARKET_OPEN_HOUR, US_MARKET_OPEN_MINUTE, US_MARKET_CLOSE_HOUR, US_MARKET_CLOSE_MINUTE
    """
    返回美股当天的开盘和收盘时间（北京时间），自动判断夏令时/冬令时。
    返回值: (open_time, close_time)，格式为字符串 'HH:MM'
    """
    now = datetime.now()
    year = now.year

    # 美股夏令时：3月第二个星期日2:00至11月第一个星期日2:00（美国时间）
    # 计算夏令时开始和结束日期（北京时间要+13小时/12小时，但只需判断日期即可）
    # 夏令时开始
    march = datetime(year, 3, 1)
    # 找到3月的第二个星期日
    first_sunday = march + timedelta(days=(6 - march.weekday()))
    second_sunday = first_sunday + timedelta(days=7)
    dst_start = second_sunday

    # 夏令时结束
    november = datetime(year, 11, 1)
    first_sunday_nov = november + timedelta(days=(6 - november.weekday()))
    dst_end = first_sunday_nov

    # 当前是否在夏令时
    if dst_start <= now < dst_end:
        US_MARKET_OPEN_HOUR = 21
        US_MARKET_OPEN_MINUTE = 30
        US_MARKET_CLOSE_HOUR = 4
        US_MARKET_CLOSE_MINUTE = 0
    else:
        US_MARKET_OPEN_HOUR = 22
        US_MARKET_OPEN_MINUTE = 30
        US_MARKET_CLOSE_HOUR = 5
        US_MARKET_CLOSE_MINUTE = 0
    
    logger.info(f"美股今日开盘时间（北京时间）：{US_MARKET_OPEN_HOUR}:{US_MARKET_OPEN_MINUTE}")
    logger.info(f"美股今日收盘时间（北京时间）：{US_MARKET_CLOSE_HOUR}:{US_MARKET_CLOSE_MINUTE}")

def get_local_trading_day(now=None):
    """
    Returns the "trading day" corresponding to the current local time.
    A trading day runs from 21:30 to 21:29 the following day.
    The returned date corresponds to the date at the start of the trading day (i.e., the date of the 21:30 boundary).
    """
    if now is None:
        now = datetime.now()
    split_time = time(US_MARKET_OPEN_HOUR, US_MARKET_OPEN_MINUTE)
    if now.time() >= split_time:
        return now.date()
    else:
        return (now - timedelta(days=1)).date()

def reset_position():
    """Reset all position-related global variables."""
    global g_position_symbol, g_position_price, g_position_quantity
    global g_position_stop_loss_price, g_position_take_profit_price, g_last_open_time
    g_position_symbol = None
    g_position_price = Decimal('0')
    g_position_quantity = Decimal('0')
    g_position_stop_loss_price = Decimal('0')
    g_position_take_profit_price = Decimal('0')
    g_last_open_time = None

def update_position(event: PushOrderChanged):
    """Update position info after a buy order is filled."""
    global g_position_symbol, g_position_price, g_position_quantity, g_last_open_time
    global g_position_stop_loss_price, g_position_take_profit_price
    g_position_symbol = event.symbol
    g_position_price = event.submitted_price
    g_position_quantity = event.executed_quantity
    g_last_open_time = datetime.now()
    g_position_stop_loss_price = (g_position_price * STOP_LOSS_RATIO).quantize(PRICE_PRECISION, rounding=ROUND_DOWN)
    g_position_take_profit_price = (g_position_price * TAKE_PROFIT_RATIO).quantize(PRICE_PRECISION, rounding=ROUND_DOWN)

def reset_daily_trade_state():
    global g_today_trades, g_today_date, g_today_profit
    today = get_local_trading_day()
    if g_today_date != today:
        g_today_trades = []
        g_today_profit = False
        g_today_date = today

def add_today_trade(action: Action, buy: float):
    """Add a new trade to today's trades."""
    global g_today_trades
    g_today_trades.append({'action': action, 'buy': buy})

def update_today_trades(event: PushOrderChanged):
    """Update today's trades."""
    global g_today_trades, g_today_profit
    if len(g_today_trades) > 0:
        g_today_trades[-1]['sell'] = event.submitted_price
        g_today_trades[-1]['profit'] = event.submitted_price >= g_today_trades[-1]['buy']
        if g_today_trades[-1]['profit']:
            g_today_profit = True

def can_trade(action: Action) -> bool:
    reset_daily_trade_state()
    # If already profitable today, do not trade
    if g_today_profit:
        return False
    # If already traded twice today, do not trade
    if len(g_today_trades) >= 2:
        return False
    # Allow the first trade
    if not g_today_trades:
        return True
    # For the second trade, the direction must be opposite
    last_action = g_today_trades[-1]['action']
    if action == last_action:
        return False
    return True

def get_option_action(event: PushOrderChanged) -> Action:
    """Get the action for the option contract."""
    if event.stock_name.lower().endswith("call"):
        return Action.BUY
    if event.stock_name.lower().endswith("put"):
        return Action.SELL
    return None

def get_current_price(action: Action, symbol: str) -> Optional[float]:
    """Get the current best ask/bid price for the given symbol."""
    try:
        resp = quote_ctx.depth(symbol)
        if action == Action.BUY and resp.asks:
            return resp.asks[0].price
        if action == Action.SELL and resp.bids:
            return resp.bids[0].price
        logger.warning("No market depth data available.")
    except Exception as e:
        logger.warning(f"Failed to fetch market depth: {e}")
    return None

def get_latest_price(symbol: str) -> Optional[float]:
    """Get the latest traded price for the symbol."""
    try:
        price_resp = quote_ctx.quote([symbol])
        for item in price_resp:
            return item.last_done
    except Exception as e:
        logger.warning(f"Failed to fetch latest price: {e}")
    return None

def get_next_expiry(symbol: str) -> Optional[date]:
    """Get the next available option expiry date."""
    try:
        date_list = quote_ctx.option_chain_expiry_date_list(symbol)
        for item in date_list:
            if date.today() <= item:
                return item
    except Exception as e:
        logger.warning(f"Failed to fetch expiry dates: {e}")
    return None

def get_option_chain(symbol: str, expiry: date):
    """Get the option chain for a given symbol and expiry date."""
    try:
        return quote_ctx.option_chain_info_by_date(symbol, expiry)
    except Exception as e:
        logger.warning(f"Failed to fetch option chain: {e}")
    return []

def select_strike_options(options, price: float, window: int = 2):
    """Select options with strike prices within a window around the current price."""
    strikes = [item.price for item in options]
    if not strikes:
        return []
    closest_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - price))
    indices = range(max(0, closest_idx - window), min(len(strikes), closest_idx + window + 1))
    return [options[i] for i in indices]

def choose_option_contract(selected_options, action: Action):
    """Choose the best option contract based on action."""
    if not selected_options:
        return None, None
    if action == Action.BUY:
        chosen = max(selected_options, key=lambda x: x.price)
        return chosen.call_symbol, chosen.price
    if action == Action.SELL:
        chosen = min(selected_options, key=lambda x: x.price)
        return chosen.put_symbol, chosen.price
    return None, None

def validate_auth(data):
    """Validate webhook token."""
    token = data.get('token')
    if token != LONGPORT_WEBHOOK_SECRET:
        raise Exception("Authentication failed: Invalid token.")

def parse_webhook_data(data):
    """Parse and validate webhook data."""
    ticker = data.get('ticker')
    action = data.get('action')
    if not ticker or not action:
        raise ValueError("Missing parameter: ticker or action.")
    try:
        action_enum = Action(action)
    except ValueError:
        raise ValueError(f"Invalid action: {action}")
    return ticker, action_enum

def is_weekend(dt: datetime) -> bool:
    """Check if the given datetime is a weekend."""
    return dt.weekday() >= 5

def get_trading_session(dt: datetime):
    """Return the open and close time for US night session."""
    if dt.hour >= US_MARKET_OPEN_HOUR:
        open_time = dt.replace(hour=US_MARKET_OPEN_HOUR, minute=US_MARKET_OPEN_MINUTE, second=0, microsecond=0)
        close_time = (dt + timedelta(days=1)).replace(hour=US_MARKET_CLOSE_HOUR, minute=US_MARKET_CLOSE_MINUTE, second=0, microsecond=0)
    elif dt.hour < US_MARKET_CLOSE_HOUR:
        open_time = (dt - timedelta(days=1)).replace(hour=US_MARKET_OPEN_HOUR, minute=US_MARKET_OPEN_MINUTE, second=0, microsecond=0)
        close_time = dt.replace(hour=US_MARKET_CLOSE_HOUR, minute=US_MARKET_CLOSE_MINUTE, second=0, microsecond=0)
    else:
        return None, None
    return open_time, close_time

def validate_active_time(active_time: datetime = None):
    """
    Only allow trading during active session.
    - No trading after 21:30 on Friday (risk of not being able to close position).
    - No trading on weekends.
    - Only allow trading during US night session (21:30-04:00), with a buffer.
    """
    dt = active_time or datetime.now()
    if dt.weekday() == 4 and dt.hour >= US_MARKET_OPEN_HOUR:
        raise Exception("No trading after 21:30 on Friday.")
    if is_weekend(dt):
        raise Exception("No trading on weekends.")
    open_time, close_time = get_trading_session(dt)
    if not open_time or not close_time:
        raise Exception("Not in US trading session.")

    # No trading shall be permitted within the first 30 minutes after the market opens and within the last 30 minutes before the market closes.
    allow_start = open_time + timedelta(minutes=30)
    allow_end = close_time - timedelta(minutes=30)
    if not (allow_start <= dt <= allow_end):
        raise Exception("Not in allowed trading time window.")

def validate_cooldown():
    """Ensure only one open position within cooldown period."""
    global g_last_open_time
    if g_last_open_time is not None:
        delta = (datetime.now() - g_last_open_time).total_seconds()
        if delta < OPEN_COOLDOWN_SECONDS:
            raise Exception(f"Cooldown not finished. {delta:.0f}s since last open.")

# ================== Trading Logic ==================
def submit_option_order(action: Action, symbol: str):
    """Submit an option order."""
    try:
        price = get_current_price(action, symbol)
        if price is None:
            logger.warning("No option price available.")
            return
        max_buy_resp = trade_ctx.estimate_max_purchase_quantity(
            symbol=symbol,
            order_type=OrderType.LO,
            side=OrderSide.Buy,
            price=price
        )
        if int(max_buy_resp.cash_max_qty) == 0:
            raise Exception("Insufficient cash.")
        order = trade_ctx.submit_order(
            symbol,
            OrderType.LO,
            OrderSide.Buy,
            max_buy_resp.cash_max_qty,
            TimeInForceType.GoodTilCanceled,
            submitted_price=price
        )
        g_pending_orders[order.order_id] = (datetime.now(), symbol)
        logger.info(f"Order submitted: {symbol}, qty: {max_buy_resp.cash_max_qty}")
    except Exception as e:
        logger.error(f"Order submission failed: {e}")

def trade_option(symbol: str, action: Action, window: int = 2):
    """Main trading flow: select and submit option order."""
    if not can_trade(action):
        logger.info("Today's trading conditions are not met. Order placement is rejected.")
        return
    price = get_latest_price(symbol)
    if price is None:
        logger.warning("Failed to get underlying price.")
        return
    expiry = get_next_expiry(symbol)
    if expiry is None:
        logger.warning("Failed to get expiry date.")
        return
    options = get_option_chain(symbol, expiry)
    if not options:
        logger.warning("No option chain data.")
        return
    selected_options = select_strike_options(options, price, window)
    for opt in selected_options:
        logger.info(f"Strike: {opt.price}, Call: {opt.call_symbol}, Put: {opt.put_symbol}")
    chosen_symbol, strike_price = choose_option_contract(selected_options, action)
    if not chosen_symbol:
        logger.warning("No suitable option contract selected.")
        return
    logger.info(f"Selected option: {chosen_symbol}, strike: {strike_price}")
    submit_option_order(action, chosen_symbol)

def set_position_risk():
    """Set stop loss and take profit orders for the current position."""
    global g_stop_order_id, g_take_profit_order_id
    try:
        stop_order = trade_ctx.submit_order(
            g_position_symbol,
            OrderType.MIT,
            OrderSide.Sell,
            g_position_quantity,
            TimeInForceType.GoodTilCanceled,
            trigger_price=g_position_stop_loss_price,
            remark="Stop Loss"
        )
        g_stop_order_id = stop_order.order_id
    except Exception as e:
        logger.warning(f"Failed to set stop loss: {e}")
    try:
        take_profit_order = trade_ctx.submit_order(
            g_position_symbol,
            OrderType.MIT,
            OrderSide.Sell,
            g_position_quantity,
            TimeInForceType.GoodTilCanceled,
            trigger_price=g_position_take_profit_price,
            remark="Take Profit"
        )
        g_take_profit_order_id = take_profit_order.order_id
    except Exception as e:
        logger.warning(f"Failed to set take profit: {e}")

def cancel_risk_orders():
    """Cancel stop loss and take profit orders."""
    global g_stop_order_id, g_take_profit_order_id
    try:
        if g_stop_order_id:
            trade_ctx.cancel_order(g_stop_order_id)
            logger.info("Stop loss order cancelled.")
    except Exception as e:
        logger.warning(f"Failed to cancel stop loss: {e}")
    finally:
        g_stop_order_id = None
    try:
        if g_take_profit_order_id:
            trade_ctx.cancel_order(g_take_profit_order_id)
            logger.info("Take profit order cancelled.")
    except Exception as e:
        logger.warning(f"Failed to cancel take profit: {e}")
    finally:
        g_take_profit_order_id = None

def log_today_trades():
    """Log today's trades."""
    logger.info(f"Today's trades: {g_today_trades}")

def auto_close_position():
    """Automatically close position at scheduled time."""
    log_today_trades()
    if g_position_symbol is None:
        logger.info("No position to close.")
        return
    try:
        trade_ctx.submit_order(
            g_position_symbol,
            OrderType.MO,
            OrderSide.Sell,
            g_position_quantity,
            TimeInForceType.GoodTilCanceled,
            outside_rth=OutsideRTH.AnyTime,
        )
        logger.info(f"Auto close order submitted: {g_position_symbol}, qty: {g_position_quantity}")
    except Exception as e:
        logger.error(f"Auto close order failed: {e}")

def check_pending_orders():
    """Check and cancel pending orders that have timed out."""
    now = datetime.now()
    timeout = timedelta(seconds=30)
    to_cancel = []
    for order_id, (submit_time, symbol) in list(g_pending_orders.items()):
        if now - submit_time > timeout:
            try:
                trade_ctx.cancel_order(order_id)
                logger.info(f"Order {order_id} cancelled due to timeout.")
            except Exception as e:
                logger.warning(f"Failed to cancel order {order_id}: {e}")
            to_cancel.append(order_id)
    for order_id in to_cancel:
        g_pending_orders.pop(order_id, None)

def on_order_changed(event: PushOrderChanged):
    """Order change callback, thread-safe."""
    with g_position_lock:
        if event.order_id in g_pending_orders and event.status in [OrderStatus.Filled, OrderStatus.Canceled]:
            g_pending_orders.pop(event.order_id, None)
        if event.status == OrderStatus.Filled:
            if event.order_id in g_processed_order_ids:
                logger.info(f"Order {event.order_id} already processed.")
                return
            g_processed_order_ids[event.order_id] = None
            if len(g_processed_order_ids) > MAX_PROCESSED_ORDERS:
                g_processed_order_ids.popitem(last=False)
            if event.side == OrderSide.Buy:
                if g_position_symbol is None:
                    logger.info(f"Buy order filled: {event.symbol}")
                    
                    add_today_trade(get_option_action(event), event.submitted_price)
                    update_position(event)
                    set_position_risk()
            elif event.side == OrderSide.Sell:
                if g_position_symbol is not None:
                    logger.info(f"Sell order filled: {g_position_symbol}")

                    update_today_trades(event)
                    reset_position()
                    cancel_risk_orders()

# ================== Flask Routes ==================
@app.route('/')
def home():
    return jsonify({'code': 200, 'status': 'success'}), 200

@app.route('/webhook', methods=['POST'])
def webhook():
    """
    Webhook endpoint for TradingView signals.
    Expected JSON:
    {
        "ticker": "TSLA.US",
        "action": "buy/sell",
        "token": "xxx"
    }
    """
    try:
        data = request.json
        logger.info(f"Received webhook: {data}")
        validate_auth(data)
        ticker, action_enum = parse_webhook_data(data)
        update_us_stock_trading_hours()
        validate_active_time()
        trade_option(ticker, action_enum)
        return jsonify({'code': 200, 'status': 'success'}), 200
    except ValueError as ve:
        logger.warning(f"Parameter error: {ve}")
        return jsonify({'code': 400, 'status': 'error', 'msg': str(ve)}), 400
    except Exception as e:
        logger.warning(f"Processing failed: {e}")
        return jsonify({'code': 500, 'status': 'error', 'msg': str(e)}), 500

# ================== Main ==================
if __name__ == '__main__':
    logger.info("Service started. Beijing time: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    trade_ctx.set_on_order_changed(on_order_changed)
    trade_ctx.subscribe([TopicType.Private])
    scheduler = BackgroundScheduler()
    scheduler.add_job(auto_close_position, 'cron', hour=US_MARKET_CLOSE_HOUR-1, minute=30)
    scheduler.add_job(check_pending_orders, 'interval', seconds=5)
    scheduler.start()
    app.run(host='0.0.0.0', port=80)