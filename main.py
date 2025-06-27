import os
import logging
import threading
from datetime import datetime, date, timedelta
from collections import OrderedDict
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from typing import Optional

from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from longport.openapi import Config, QuoteContext, TradeContext, PushOrderChanged, OrderType, OrderStatus
from longport.openapi import OrderSide, TimeInForceType, Period, AdjustType, TradeSessions, TopicType, OutsideRTH

# ====== 日志配置区 ======
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ====== 枚举，ClassData区 ======
class Action(Enum):
    BUY = "buy"
    SELL = "sell"


# ====== 环境变量区 ======
LONGPORT_WEBHOOK_SECRET = os.getenv("LONGPORT_WEBHOOK_SECRET")
CONFIG = Config.from_env()


# ====== 配置参数区 ======
OPEN_COOLDOWN_MINUTES = 10 # 冷却时间，单位分钟
OPEN_COOLDOWN_SECONDS = OPEN_COOLDOWN_MINUTES * 60  # 换算成秒
TAKE_PROFIT_RATIO = Decimal('1.05')  # 止盈比例（+5%）
STOP_LOSS_RATIO = Decimal('0.95') # 止损比例（-5%）
PRICE_PRECISION = Decimal('0.01')  # 价格精度，小数点后两位
MAX_PROCESSED_ORDERS = 1000 # 已处理订单最大缓存数


# ====== 其他全局变量 ======
last_open_time = None # 记录上一次开仓时间
position_symbol = None # 开仓标的
position_price = Decimal('0') # 开仓价格
position_stop_loss_price = Decimal('0') # 止损平仓价格
position_take_profit_price = Decimal('0') # 止盈平仓价格
stop_order_id = None # 止损订单ID
take_profit_order_id = None # 止盈订单ID
position_quantity = Decimal('0') # 开仓数量
position_lock = threading.Lock()
processed_order_ids = OrderedDict()

quote_ctx = QuoteContext(CONFIG)
trade_ctx = TradeContext(CONFIG)
app = Flask(__name__)

# ====== 函数区 ======
def set_position_info(event: PushOrderChanged, sell: bool = False):
    """
    设置开仓信息
    """
    global position_symbol, position_price, position_quantity, last_open_time
    global position_stop_loss_price, position_take_profit_price
    if sell:
        logger.info("================开始重置订单信息================")
        position_symbol = None
        position_price = Decimal('0')
        position_quantity = Decimal('0')
        position_stop_loss_price = Decimal('0')
        position_take_profit_price = Decimal('0')
        last_open_time = None
        logger.info("================重置订单信息成功================")
    else:
        logger.info("================开始添加下单信息================")
        position_symbol = event.symbol
        position_price = event.submitted_price
        position_quantity = event.executed_quantity
        last_open_time = datetime.now()

        print("原始价格:" + str(position_price))
        position_stop_loss_price = (position_price * STOP_LOSS_RATIO).quantize(PRICE_PRECISION, rounding=ROUND_DOWN)
        print("止损价格:" + str(position_stop_loss_price))

        # 每笔交易2.5%止盈
        position_take_profit_price = (event.submitted_price * TAKE_PROFIT_RATIO).quantize(PRICE_PRECISION, rounding=ROUND_DOWN)
        print("止盈价格:" + str(position_take_profit_price))

        logger.info("================添加下单信息成功================")

def auto_close_position():
    """每天定时自动平仓"""
    try:
        if position_symbol is None:
            logger.info("没有持仓，跳过平仓")
            return
        trade_ctx.submit_order(
            position_symbol,
            OrderType.MO,
            OrderSide.Sell,
            position_quantity,
            TimeInForceType.GoodTilCanceled,
            outside_rth=OutsideRTH.AnyTime,
        )
        logger.info(f"平仓下单完成 - 股票：{position_symbol}，数量：{str(position_quantity)}")
    except Exception as e:
        logger.error(f"下单失败: {e}")

def set_position_risk():
    """
    设置止盈止损
    """
    global stop_order_id, take_profit_order_id
    try:
        stop_order = trade_ctx.submit_order(
            position_symbol,
            OrderType.MIT,
            OrderSide.Sell,
            position_quantity,
            TimeInForceType.GoodTilCanceled,
            trigger_price=position_stop_loss_price,
            remark="止损",
        )
        stop_order_id = stop_order.order_id
    except Exception as e:
        logger.warning(f"增加止损订单失败: {e}")

    try:
        take_profit_order = trade_ctx.submit_order(
            position_symbol,
            OrderType.MIT,
            OrderSide.Sell,
            position_quantity,
            TimeInForceType.GoodTilCanceled,
            trigger_price=position_take_profit_price,
            remark="止盈",
        )
        take_profit_order_id = take_profit_order.order_id
    except Exception as e:
        logger.warning(f"增加止盈订单失败: {e}")


def cancel_position_risk_order():
    """
    自动撤销止损止盈的监听
    """
    global stop_order_id, take_profit_order_id
    try:
        trade_ctx.cancel_order(stop_order_id)
        logger.info("自动撤销止损订单成功")
    except Exception as e:
        logger.warning(f"撤销止损监听失败: {e}")
    finally:
        stop_order_id = None

    try:
        trade_ctx.cancel_order(take_profit_order_id)
        logger.info("自动撤销止盈订单成功")
    except Exception as e:
        logger.warning(f"撤销止盈监听失败: {e}")
    finally:
        take_profit_order_id = None

def on_order_changed(event: PushOrderChanged):
    with position_lock:
        if event.status == OrderStatus.Filled:
            global processed_order_ids
            logger.info(f"on_order_changed: {event.order_id}")
            if event.order_id in processed_order_ids:
                logger.info(f"订单 {event.order_id} 已处理，跳过")
                return
            
            # processed_order_ids 这个 set 会随着订单数量的增加而无限增长，最终可能导致内存泄漏或占用过多内存。
            processed_order_ids[event.order_id] = None
            if len(processed_order_ids) > MAX_PROCESSED_ORDERS:
                processed_order_ids.popitem(last=False)

            if event.side == OrderSide.Buy:
                if position_symbol is None:
                    logger.info(f"发现买入订单:{event.symbol}")
                    set_position_info(event)
                    set_position_risk()
            elif event.side == OrderSide.Sell:
                if position_symbol is not None:
                    logger.info(f"发现卖出订单:{position_symbol}")
                    set_position_info(event, sell=True)
                    cancel_position_risk_order()
            else:
                pass

def get_current_price(action: Action, symbol: str) -> Optional[float]:
    """获取当前盘口价格"""
    try:
        resp = quote_ctx.depth(symbol)
        if resp.asks and resp.bids:
            if action == Action.BUY:
                price = resp.asks[0].price
                if price is not None:
                    return price
                logger.warning("可能为夜盘，卖一价为空")
            elif action == Action.SELL:
                price = resp.bids[0].price
                if price is not None:
                    return price
                logger.warning("可能为夜盘，买一价为空")
        else:
            logger.warning("当前无盘口数据...")
    except Exception as e:
        logger.warning(f"查询盘口失败: {e}")
    return None

def get_underlying_price(symbol: str) -> Optional[float]:
    """获取标的最新成交价"""
    try:
        price_resp = quote_ctx.quote([symbol])
        for item in price_resp:
            return item.last_done
    except Exception as e:
        logger.warning(f"标的价格查询失败: {e}")
    return None

def get_target_expiry_date(symbol: str) -> Optional[date]:
    """获取下一个可用的期权到期日"""
    try:
        date_list = quote_ctx.option_chain_expiry_date_list(symbol)
        for item in date_list:
            if date.today() <= item:
                return item
    except Exception as e:
        logger.warning(f"查询期权日期失败: {e}")
    return None

def get_option_chain_by_date(symbol: str, expiry: date):
    """获取指定到期日的期权链信息"""
    try:
        return quote_ctx.option_chain_info_by_date(symbol, expiry)
    except Exception as e:
        logger.warning(f"查询期权链失败: {e}")
    return []

def select_options_by_strike(options, current_price: float, window: int = 2):
    """选取行权价在当前价格上下window档的期权"""
    strikes = [item.price for item in options]
    if not strikes:
        return []
    closest_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - current_price))
    selected_indices = range(max(0, closest_idx - window), min(len(strikes), closest_idx + window + 1))
    return [options[i] for i in selected_indices]

def choose_option(selected_options, action: Action):
    """根据操作选择合适的期权合约symbol"""
    if not selected_options:
        return None, None
    if action == Action.BUY:
        chosen = max(selected_options, key=lambda x: x.price)
        return chosen.call_symbol, chosen.price
    elif action == Action.SELL:
        chosen = min(selected_options, key=lambda x: x.price)
        return chosen.put_symbol, chosen.price
    return None, None

def submit_option_order(action: Action, symbol: str):
    """提交期权买入订单"""
    try:
        current_price = get_current_price(action, symbol)
        if current_price is None:
            logger.warning("没有查询到期权价格")
            return
        logger.info(f"当前期权价格为: {current_price}")
        max_buy_resp = trade_ctx.estimate_max_purchase_quantity(
            symbol=symbol,
            order_type=OrderType.LO,
            side=OrderSide.Buy,
            price=current_price
        )
        if int(max_buy_resp.cash_max_qty) == 0:
            raise Exception("现金不够")

        logger.info(f"当前期权最大买入数量: {str(max_buy_resp.cash_max_qty)}")
        trade_ctx.submit_order(
            symbol,
            OrderType.LO,
            OrderSide.Buy,
            max_buy_resp.cash_max_qty,
            TimeInForceType.GoodTilCanceled,
            submitted_price=current_price
        )
        logger.info(f"下单完成 - 股票：{symbol}，数量：{str(max_buy_resp.cash_max_qty)}")
    except Exception as e:
        logger.error(f"下单失败: {e}")

def trade_option(symbol: str, action: Action, window: int = 2):
    """主流程：选择合适期权并下单"""
    # 1. 获取标的价格
    current_price = get_underlying_price(symbol)
    if current_price is None:
        logger.warning("标的价格查询失败")
        return
    logger.info(f"当前标的价格为: {current_price}")

    # 2. 获取期权到期日
    expiry = get_target_expiry_date(symbol)
    if expiry is None:
        logger.warning("查询期权日期失败")
        return
    logger.info(f"目标期权日期为: {expiry}")

    # 3. 获取期权链
    options = get_option_chain_by_date(symbol, expiry)
    if not options:
        logger.warning("未获取到期权链信息")
        return

    # 4. 选取合适的期权
    selected_options = select_options_by_strike(options, current_price, window)
    logger.info("选中的期权档位：")
    for opt in selected_options:
        logger.info(f"行权价: {opt.price}, Call: {opt.call_symbol}, Put: {opt.put_symbol}")

    chosen_symbol, strike_price = choose_option(selected_options, action)
    if not chosen_symbol:
        logger.warning("未选中合适的期权合约")
        return

    logger.info(f"选择下单的期权: {chosen_symbol} 行权价: {strike_price}")
    submit_option_order(action, chosen_symbol)

def validate_position_time_range():
    """固定时间内只开仓一次"""
    global last_open_time
    if last_open_time is not None:
        delta = (datetime.now() - last_open_time).total_seconds()
        if delta < OPEN_COOLDOWN_SECONDS:
            raise Exception(f"冷却时间未到，拒绝开仓。距离上次开仓{delta}秒")

def is_weekend(dt: datetime) -> bool:
    return dt.weekday() >= 5

def get_trading_session(dt: datetime):
    """返回美股夜盘的开盘和收盘时间"""
    if dt.hour >= 21:
        open_time = dt.replace(hour=21, minute=30, second=0, microsecond=0)
        close_time = (dt + timedelta(days=1)).replace(hour=4, minute=0, second=0, microsecond=0)
    elif dt.hour < 4:
        open_time = (dt - timedelta(days=1)).replace(hour=21, minute=30, second=0, microsecond=0)
        close_time = dt.replace(hour=4, minute=0, second=0, microsecond=0)
    else:
        return None, None
    return open_time, close_time

def validate_active_time(active_time: datetime = None):
    """只在交易活跃期间开仓，以及末日期权无法平仓的风险"""
    dt = active_time or datetime.now()

    if is_weekend(dt):
        raise Exception("周末不允许开仓")

    open_time, close_time = get_trading_session(dt)
    if not open_time or not close_time:
        raise Exception("当前不在美股盘中时间段内，拒绝开仓")

    # allow_start = open_time + timedelta(minutes=30)  # 22:00
    allow_start = open_time  # 21:30
    allow_end = close_time - timedelta(minutes=30)   # 03:30

    if not (allow_start <= dt <= allow_end):
        raise Exception("当前不在允许开仓时间段内，拒绝开仓")

def parse_webhook_data(data):
    """解析并校验 webhook 数据"""
    ticker = data.get('ticker')
    action = data.get('action')
    if not ticker or not action:
        raise ValueError("参数缺失：ticker 或 action")
    try:
        action_enum = Action(action)
    except ValueError:
        raise ValueError(f"无效的 action: {action}")
    return ticker, action_enum

def validate_auth(data):
    token = data.get('token')
    if token != LONGPORT_WEBHOOK_SECRET:
        raise Exception("鉴权失败: Token 不正确")


@app.route('/')
def home():
    return jsonify({'code':200, 'status': 'success'}), 200


@app.route('/webhook', methods=['POST'])
def webhook():
    """
    {
        "ticker": "TSLA.US",
        "action": "buy/sell",
        "token": "xxx"
    }
    """
    try:
        webhook_data = request.json
        logger.info(f"收到 TradingView 信号: {webhook_data}")
        validate_auth(webhook_data)
        ticker, action_enum = parse_webhook_data(webhook_data)

        # 冷静期代码移动到策略中了，这里暂时注释
        # validate_position_time_range()
        validate_active_time()
        trade_option(ticker, action_enum)

        return jsonify({'code':200, 'status': 'success'}), 200
    except ValueError as ve:
        logger.warning(f"参数错误: {ve}")
        return jsonify({'code': 400, 'status': 'error', 'msg': str(ve)}), 400
    except Exception as e:
        logger.warning(f"处理失败: {e}")
        return jsonify({'code':500, 'status': 'error', 'msg': str(e)}), 500

@app.route('/webhook_test', methods=['POST'])
def webhook_test():
    webhook_data = request.json
    logger.info(f"收到 TradingView 信号: {webhook_data}")

    return jsonify({'code':200, 'status': 'success'}), 200



if __name__ == '__main__':
    logger.info("启动成功，当前北京时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    
    trade_ctx.set_on_order_changed(on_order_changed)
    trade_ctx.subscribe([TopicType.Private])

    scheduler = BackgroundScheduler()

    scheduler.add_job(auto_close_position, 'cron', hour=3, minute=30)
    scheduler.start()
    app.run(host='0.0.0.0', port=80)