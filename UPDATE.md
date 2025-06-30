# 待更新

## 1. 部分成交

当前 on_order_changed 的逻辑只在订单状态为 Filled 时才会处理，并且直接把成交数量（event.executed_quantity）作为持仓数量（position_quantity）记录。如果下单 10 张，只成交 5 张，剩下 5 张挂单未成交，这种“部分成交”场景下，OrderStatus 可能是 PartiallyFilled，而不是 Filled，只有全部成交才会变成 Filled。

### 问题

- 只处理 Filled 状态：部分成交时不会触发持仓信息的更新，只有全部成交才会更新。
- 持仓数量不准确：如果只处理 Filled，那部分成交时 position_quantity 还是 0，直到全部成交才变成 10。
- 止盈止损单的下单时机：如果只在全部成交后下止盈止损单，部分成交期间没有风控保护。

### 期权下单的常见状态

- PartiallyFilled：部分成交
- Filled：全部成交

### 推荐的处理方式

- 在 PartiallyFilled 和 Filled 都要处理：每次有成交（不管是部分还是全部），都要更新持仓信息。
- 每次回调都要把 position_quantity 累加（而不是直接赋值），这样才能反映真实持仓。
- 可以在第一次有成交时就挂止盈止损单，或者每次成交都检查/调整止盈止损单。
