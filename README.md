[🇨🇳 中文文档](./README.zh.md)

# Longbridge Securities Intraday Options Quantitative Trading System

## Introduction

This project is an intraday options quantitative trading system based on the Longbridge Securities API. It supports automated order placement, take-profit/stop-loss, risk control, and is suitable for highly liquid US stock options (e.g., TSLA.US).

## Entry

Find a suitable scalping strategy on TradingView, use TradingView alerts to send instructions (webhook), and the program will receive long/short signals and execute opening, take-profit, and stop-loss logic. My strategy is in the Pine folder, using an RSI reversal pattern. The drawback is that bottom-fishing may not catch the absolute bottom, and shorting may not catch the absolute top.

## Exit

Set take-profit and stop-loss immediately after opening a position. Both are set at a fixed 3% for closing, with a risk-reward ratio of 1:1, targeting 1-2 candlesticks.

If the option price moves sideways after buying (neither hitting take-profit nor stop-loss), the system will automatically close your position at 3:30 AM every day (even if neither take-profit nor stop-loss is triggered). This prevents indefinite holding, avoiding time value decay and overnight risk.

## Installation

### Environment

```sh
python >= 3.8
```

### Clone the repository

```sh
git clone https://github.com/leyen-me/TradingRisk.git
```

### Install dependencies

```sh
pip install -r requirements.txt
```

### Configure environment variables

```env
LONGPORT_APP_KEY=xxx
LONGPORT_APP_SECRET=xxx
LONGPORT_ACCESS_TOKEN=xxx
# These three variables are obtained from the official Longbridge SDK. Log in and activate your account. Note: LONGPORT_ACCESS_TOKEN distinguishes between simulation and real accounts.

LONGPORT_WEBHOOK_SECRET=xxx
# This variable is custom for this program to prevent API abuse. Set it to any password (6-12 characters recommended).
```

### Start

```sh
python main.py
```

### Deployment

This project supports both native and Docker deployment.

## Disclaimer

Trading involves risk. Please use a simulation account for testing first. The investment logic and code in this project do not constitute investment advice.

## Contribution

Issues and PRs are welcome to improve this project.

## License

This project is open-sourced under the MIT License.
