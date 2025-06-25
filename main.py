import time
import logging
import threading
from datetime import datetime
from collections import OrderedDict
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from longport.openapi import Config, QuoteContext, TradeContext, PushOrderChanged, OrderType, OrderStatus
from longport.openapi import OrderSide, TimeInForceType, Period, AdjustType, TradeSessions, TopicType

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

config = Config.from_env()
quote_ctx = QuoteContext(config)
trade_ctx = TradeContext(config)


position_symbol = None                      # 开仓标的
position_price = Decimal('0')               # 开仓价格
position_stop_loss_price = Decimal('0')     # 止损平仓价格
position_take_profit_price = Decimal('0')   # 止盈平仓价格
position_quantity = Decimal('0')            # 开仓数量

position_lock = threading.Lock()

MAX_PROCESSED_ORDERS = 1000
processed_order_ids = OrderedDict()


def get_min_20_price():
    lowest = None
    try:
        arr = []
        resp = quote_ctx.candlesticks(position_symbol, Period.Min_2, 20, AdjustType.NoAdjust, TradeSessions.Intraday)
        for item in resp:
            arr.append(item.low)
        lowest = min([item.low for item in resp])
        logging.info(f"查询到最近20根K线的最低价:{str(lowest)}")
    except Exception as e:
        logger.warning(f"查询最低价失败: {e}")
    return lowest

def set_position_info(event: PushOrderChanged, sell: bool = False):
    """
    设置开仓信息
    """
    global position_symbol, position_price, position_quantity
    global position_stop_loss_price, position_take_profit_price
    if sell:
        position_symbol = None
        position_price = Decimal('0')
        position_quantity = Decimal('0')
        position_stop_loss_price = Decimal('0')
        position_take_profit_price = Decimal('0')
        logger.info("重置订单信息成功")
    else:
        position_symbol = event.symbol
        position_price = event.submitted_price
        position_quantity = event.executed_quantity

        price = get_min_20_price()
        if price is None:
            position_stop_loss_price = event.submitted_price * Decimal('0.9').quantize(Decimal('0.01'), rounding=ROUND_DOWN)
        else:
            position_stop_loss_price = price
        
        position_take_profit_price = event.submitted_price * Decimal('1.1').quantize(Decimal('0.01'), rounding=ROUND_UP)
        logger.info("添加下单信息成功")

def set_position_risk():
    """
    设置止盈止损，风险回报比为1:1。
    """
    try:
        trade_ctx.submit_order(
            position_symbol,
            OrderType.MIT,
            OrderSide.Sell,
            position_quantity,
            TimeInForceType.GoodTilCanceled,
            trigger_price=position_stop_loss_price,
            remark="止损",
        )
    except Exception as e:
        logger.warning(f"增加止损订单失败: {e}")

    try:
        trade_ctx.submit_order(
            position_symbol,
            OrderType.MIT,
            OrderSide.Sell,
            position_quantity,
            TimeInForceType.GoodTilCanceled,
            trigger_price=position_take_profit_price,
            remark="止盈",
        )
    except Exception as e:
        logger.warning(f"增加止盈订单失败: {e}")


def on_order_changed(event: PushOrderChanged):
    with position_lock:
        global processed_order_ids
        logger.info(f"on_order_changed: {event.order_id}")
        if event.order_id in processed_order_ids:
            logger.info(f"订单 {event.order_id} 已处理，跳过")
            return
        
        # processed_order_ids 这个 set 会随着订单数量的增加而无限增长，最终可能导致内存泄漏或占用过多内存。
        processed_order_ids[event.order_id] = None
        if len(processed_order_ids) > MAX_PROCESSED_ORDERS:
            processed_order_ids.popitem(last=False)
        
        if event.side == OrderSide.Buy and event.status == OrderStatus.Filled:
            if position_symbol is None:
                logger.info(f"发现买入订单:{event.symbol}")
                set_position_info(event)
                set_position_risk()
        elif event.side == OrderSide.Sell and event.status == OrderStatus.Filled:
            if position_symbol is not None:
                logger.info(f"发现卖出订单:{position_symbol}")
                set_position_info(event, sell=True)
        else:
            pass

trade_ctx.set_on_order_changed(on_order_changed)
trade_ctx.subscribe([TopicType.Private])

logger.info("启动成功，当前北京时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

try:
    while True:
        time.sleep(60)
except KeyboardInterrupt:
    logger.info("检测到退出信号，正在关闭...")
finally:
    try:
        trade_ctx.unsubscribe([TopicType.Private])
    except Exception as e:
        logger.warning(f"取消交易订阅失败: {e}")
    logger.info("程序已退出。")