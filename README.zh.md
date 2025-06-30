[🇺🇸 English Version](./README.md)

# 长桥证券日内期权量化交易系统

## 介绍

本项目是一个基于长桥证券 API 的日内期权量化交易系统，支持自动化下单、止盈止损、风控等功能，适用于高流动性美股期权（如 TSLA.US）。

## 入场

在TradingView找到适合的剥头皮策略，利用TradingView的警报发出指令（webhook），然后程序接收做多、做空指令，执行开仓止盈止损等逻辑。我的策略在Pine文件夹下，采用RSI反转形态，缺点是抄底会抄在半山腰，做空会做到半山腰。

## 出场

弱水三千，只取一瓢。

开仓后立即设置止盈止损，止盈止损设置为3%固定平仓，盈亏比为1:1，目标为1~2根K线。

如果买入后，期权一直横盘，既不止盈也不止损，系统会在每天凌晨 3:30 自动平掉你的持仓（即使没有触发止盈止损）。防止无限期持仓，避免时间价值损耗和隔夜风险。

## 项目安装

### 安装环境

```sh
python >= 3.8
```

### 拉取代码

```sh
git clone https://github.com/leyen-me/TradingRisk.git
```

### 安装依赖

```sh
pip install -r requirements.txt
```

### 配置环境变量

```env
LONGPORT_APP_KEY=xxx
LONGPORT_APP_SECRET=xxx
LONGPORT_ACCESS_TOKEN=xxx
# https://open.longportapp.com/zh-CN/account
# 这三个环境变量由长桥官方SDK获得，登录账号开通即可。其中LONGPORT_ACCESS_TOKEN会区分模拟账户和综合账户。

LONGPORT_WEBHOOK_SECRET=xxx
# 这个环境变量是本程序自定义的，目的是为了防止接口被滥用，可以随意设置成任何密码就行，最好（6-12位数）
```

### 启动

```sh
python main.py
```

### 部署

本项目支持原生部署和Docker部署。

## 说明

交易有风险，投资需谨慎。建议先使用模拟仓试盘，本项目相关投资逻辑和代码不构成投资建议。

## 贡献

欢迎提交 issue 和 PR 改进本项目。

## 许可证

本项目基于 MIT License 开源。