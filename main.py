from decimal import ROUND_DOWN, ROUND_UP, Decimal
from datetime import datetime
from longport.openapi import Config, QuoteContext, TradeContext, PushOrderChanged, OrderType
from longport.openapi import PushQuote, SubType, TopicType, OrderSide, TimeInForceType
from flask import Flask

app = Flask(__name__)

config = Config.from_env()
quote_ctx = QuoteContext(config)
trade_ctx = TradeContext(config)

position_symbol = None                      # 开仓标的
position_price = Decimal('0')               # 开仓价格
position_stop_loss_price = Decimal('0')     # 止损平仓价格
position_stop_loss_10_price = Decimal('0')  # 10%
position_take_profit_price = Decimal('0')   # 止盈平仓价格
position_quantity = Decimal('0')            # 开仓数量

stop_loss_order_id = None                   # 止损订单ID
stop_loss_order_update = False              # 是否上调过止损线

def set_position_info(event: PushOrderChanged, sell: bool = False):
    """
    设置开仓信息
    """
    global position_symbol, position_price, position_quantity
    global position_stop_loss_price, position_take_profit_price, position_stop_loss_10_price
    global stop_loss_order_id, stop_loss_order_update
    if sell:
        position_symbol = None
        position_price = Decimal('0')
        position_quantity = Decimal('0')
        position_stop_loss_price = Decimal('0')
        position_stop_loss_10_price = Decimal('0')
        position_take_profit_price = Decimal('0')
        stop_loss_order_id = None
        stop_loss_order_update = False
        print("重置订单信息成功")
    else:
        position_symbol = event.symbol
        position_price = event.submitted_price
        position_quantity = event.executed_quantity
        position_stop_loss_price = event.submitted_price * Decimal('0.8').quantize(Decimal('0.01'), rounding=ROUND_DOWN)
        position_stop_loss_10_price = event.submitted_price * Decimal('1.1').quantize(Decimal('0.01'), rounding=ROUND_DOWN)
        position_take_profit_price = event.submitted_price * Decimal('1.2').quantize(Decimal('0.01'), rounding=ROUND_UP)
        print("添加下单信息成功")

def set_position_risk():
    global stop_loss_order_id
    """
    设置止盈止损，风险回报比为1:1。
    """
    try:
        stop_loss_order = trade_ctx.submit_order(
            position_symbol,
            OrderType.MIT,
            OrderSide.Sell,
            position_quantity,
            TimeInForceType.GoodTilCanceled,
            trigger_price=position_stop_loss_price,
            remark="止损",
        )
        stop_loss_order_id = stop_loss_order.order_id
    except Exception as e:
        print(f"增加止损订单失败: {e}")

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
        print(f"增加止盈订单失败: {e}")

def set_position_risk_to_open():
    global stop_loss_order_update
    try:
        stop_loss_order_update = True
        trade_ctx.replace_order(
            order_id = stop_loss_order_id,
            trigger_price = position_price,
        )
    except Exception as e:
        print(f"修改订单失败: {e}")

def on_quote(symbol: str, event: PushQuote):
    if position_symbol is None:
        return
    if stop_loss_order_id is None:
        return
    if stop_loss_order_update == True:
        return
    if symbol == position_symbol and event.last_done > position_stop_loss_10_price:
        set_position_risk_to_open()

def on_order_changed(event: PushOrderChanged):
    if str(event.side) == "OrderSide.Buy" and str(event.status) == "OrderStatus.Filled":
        if position_symbol is None:
            set_position_info(event)
            set_position_risk()

            print("开始监听股票涨幅")
            quote_ctx.subscribe([event.symbol], [SubType.Quote], is_first_push = True)
    elif str(event.side) == "OrderSide.Sell" and str(event.status) == "OrderStatus.Filled":
        if position_symbol is not None:
            set_position_info(event, sell=True)

            print("取消监听股票涨幅")
            quote_ctx.unsubscribe([event.symbol], [SubType.Quote])
    else:
        pass

@app.route("/")
def health():
    return "ok"

quote_ctx.set_on_quote(on_quote)

trade_ctx.set_on_order_changed(on_order_changed)
trade_ctx.subscribe([TopicType.Private])

print("启动成功，当前北京时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
app.run(host='0.0.0.0', port=80, debug=True)