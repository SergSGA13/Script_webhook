#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sync Futures Strategy v29.1 results to Google Sheets through an Apps Script Web App.

Tabs written:
- FUT_STRAT
- FUT_STRAT_PF
- FUT_STRAT_SL
- FUT_STRAT_SL_PF
- FUT_STRAT_SLT
- FUT_STRAT_SLT_PF

Required environment variables:
- SHEET_WEBHOOK_URL: Apps Script Web App URL
- SHEET_SECRET: same secret as FUTURES_SYNC_SECRET in apps_script_webhook.gs

Optional environment variables:
- FUTURES_TOP_N, default 30
- FUTURES_HISTORY_DAYS, default 90
- FUTURES_INTERVAL, default 15m
"""

import datetime as dt
import json
import os
import sys
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import requests

BINANCE_FAPI = "https://fapi.binance.com"

CFG = dict(
    top_n=int(os.getenv("FUTURES_TOP_N", "30")),
    symbols=[s.strip().upper() for s in os.getenv("FUTURES_SYMBOLS", "").split(",") if s.strip()],
    interval=os.getenv("FUTURES_INTERVAL", "15m"),
    tf_min=15,
    history_days=int(os.getenv("FUTURES_HISTORY_DAYS", "90")),
    capital=100.0,
    lot_frac=0.25,
    reinvest=True,
    fee_per_side=0.0008,
    leverage=1.0,
    max_lots=10,
    write_min_sets=0,
    portfolio_deposit=1000.0,
    port_risk_pct=0.25,
    stop_loss_pct=0.0,
    take_profit_pct=0.0,
    use_trend_filter=False,
    trend_len=200,
)

IND = dict(
    len=200,
    h=8.0,
    mult_buy=3.0,
    mult_sell=3.0,
    impulse_on=True,
    impulse_thr=1.0,
    impulse_lb=5,
    sweep_on=True,
    sweep_bars=20,
    check_min=10,
)

HEADERS = [
    "symbol", "tf", "signals", "sets", "wins", "losses", "winrate",
    "pnl_pct", "realized_pct", "unreal_pct", "pos_side", "pos_lots", "pos_avg",
    "last_side", "quote_volume", "updated", "lot_entries", "sets_json",
    "max_dd", "expectancy",
]

PF_HEADERS = ["metric", "value"]

PRESETS = {
    "base": dict(rules={}, tab="FUT_STRAT", pf_tab="FUT_STRAT_PF"),
    "sl": dict(rules=dict(stop_loss_pct=5.0), tab="FUT_STRAT_SL", pf_tab="FUT_STRAT_SL_PF"),
    "slt": dict(rules=dict(stop_loss_pct=5.0, use_trend_filter=True, trend_len=200), tab="FUT_STRAT_SLT", pf_tab="FUT_STRAT_SLT_PF"),
}


def rolling_mean(a: np.ndarray, w: int) -> np.ndarray:
    c = np.cumsum(np.insert(a, 0, 0.0))
    out = np.full(len(a), np.nan)
    if len(a) >= w:
        out[w - 1:] = (c[w:] - c[:-w]) / w
    return out


def rolling_lowest(a: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(a), np.nan)
    n = len(a)
    if n >= w:
        from numpy.lib.stride_tricks import sliding_window_view
        out[w - 1:] = sliding_window_view(a, w).min(axis=1)
    for i in range(min(w - 1, n)):
        out[i] = a[:i + 1].min()
    return out


def rolling_highest(a: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(a), np.nan)
    n = len(a)
    if n >= w:
        from numpy.lib.stride_tricks import sliding_window_view
        out[w - 1:] = sliding_window_view(a, w).max(axis=1)
    for i in range(min(w - 1, n)):
        out[i] = a[:i + 1].max()
    return out


def compute_signals(close: np.ndarray, high: np.ndarray, low: np.ndarray, tf_min: int) -> List[Tuple[int, str]]:
    n = len(close)
    p = IND
    idx = np.arange(p["len"])
    coefs = np.exp(-(idx ** 2) / (2 * p["h"] * p["h"]))
    den = coefs.sum()
    num = np.convolve(close, coefs)[:n]
    out = num / den if den != 0 else np.full(n, np.nan)
    mae = rolling_mean(np.abs(close - out), p["len"])
    upper = out + mae * p["mult_sell"]
    lower = out - mae * p["mult_buy"]

    lo_lowest = rolling_lowest(low, p["sweep_bars"])
    hi_highest = rolling_highest(high, p["sweep_bars"])

    bars_to_check = max(1, round(p["check_min"] / tf_min))
    signals: List[Tuple[int, str]] = []
    last_long = last_short = -10 ** 9
    sb = p["sweep_bars"]
    lb = p["impulse_lb"]

    for i in range(1, n):
        if np.isnan(upper[i]) or np.isnan(lower[i]) or np.isnan(upper[i - 1]) or np.isnan(lower[i - 1]):
            continue

        cross_under = close[i] < lower[i] and close[i - 1] >= lower[i - 1]
        cross_over = close[i] > upper[i] and close[i - 1] <= upper[i - 1]

        can_long = can_short = True
        if p["impulse_on"] and i >= lb:
            base = close[i - lb]
            delta = abs((close[i] - base) / base) * 100.0
            if delta >= p["impulse_thr"]:
                can_long = close[i] <= base
                can_short = close[i] >= base

        swept_low = i >= sb and lo_lowest[i] < lo_lowest[i - sb]
        swept_high = i >= sb and hi_highest[i] > hi_highest[i - sb]
        sweep_ok_long = (not p["sweep_on"]) or swept_low
        sweep_ok_short = (not p["sweep_on"]) or swept_high

        if cross_under and can_long and sweep_ok_long and (i - last_long) >= bars_to_check:
            last_long = i
            signals.append((i, "long"))
        if cross_over and can_short and sweep_ok_short and (i - last_short) >= bars_to_check:
            last_short = i
            signals.append((i, "short"))

    signals.sort(key=lambda s: s[0])
    return signals


def ema(arr: np.ndarray, length: int) -> np.ndarray:
    a = np.asarray(arr, dtype=float)
    out = np.empty_like(a)
    k = 2.0 / (length + 1.0)
    out[0] = a[0]
    for i in range(1, len(a)):
        out[i] = a[i] * k + out[i - 1] * (1.0 - k)
    return out


def simulate(close: np.ndarray, signals: Sequence[Tuple[int, str]], cfg: Dict, times: Optional[np.ndarray] = None) -> Dict:
    eq = cfg["capital"]
    fee = cfg["fee_per_side"]
    lev = cfg["leverage"]
    cap = cfg["max_lots"]
    sl = cfg.get("stop_loss_pct", 0.0) or 0.0
    tp = cfg.get("take_profit_pct", 0.0) or 0.0
    use_trend = bool(cfg.get("use_trend_filter"))
    trend = ema(close, int(cfg.get("trend_len", 200))) if use_trend else None

    pos = None
    set_pnl = 0.0
    eq_open = eq
    sets_pct: List[float] = []
    sets_ts: List[int] = []
    peak_eq = eq
    max_dd = 0.0
    sig = {i: side for (i, side) in signals}

    def lot_notional() -> float:
        base = eq if cfg["reinvest"] else cfg["capital"]
        return cfg["lot_frac"] * base * lev

    def open_pos(side: str, price: float) -> None:
        nonlocal eq, set_pnl, pos, eq_open
        eq_open = eq
        note = lot_notional()
        qty = note / price
        eq -= note * fee
        set_pnl = -note * fee
        pos = {"side": side, "lots": [(price, qty)]}

    def cur_set_pct(price: float) -> float:
        if not pos or not eq_open:
            return 0.0
        gross = 0.0
        for (p, q) in pos["lots"]:
            gross += q * (price - p) if pos["side"] == "long" else q * (p - price)
        return (set_pnl + gross) / eq_open * 100.0

    def close_pos(price: float, t: Optional[int]) -> None:
        nonlocal eq, set_pnl, pos, peak_eq, max_dd
        realized = 0.0
        close_note = 0.0
        for (p, q) in pos["lots"]:
            realized += q * (price - p) if pos["side"] == "long" else q * (p - price)
            close_note += q * price
        close_fee = close_note * fee
        eq += realized - close_fee
        set_pnl += realized - close_fee
        sets_pct.append(set_pnl / eq_open * 100.0 if eq_open else 0.0)
        sets_ts.append(int(t) if t is not None else 0)
        peak_eq = max(peak_eq, eq)
        if peak_eq > 0:
            max_dd = max(max_dd, (peak_eq - eq) / peak_eq * 100.0)
        pos = None

    for i, price in enumerate(close):
        t_i = int(times[i]) if times is not None else None
        if pos is not None and (sl or tp):
            current_pct = cur_set_pct(float(price))
            if sl and current_pct <= -sl:
                close_pos(float(price), t_i)
                continue
            if tp and current_pct >= tp:
                close_pos(float(price), t_i)
                continue

        side = sig.get(i)
        if side is None:
            continue

        if use_trend:
            ok = (price > trend[i]) if side == "long" else (price < trend[i])
            if not ok:
                if pos is not None and pos["side"] != side:
                    close_pos(float(price), t_i)
                continue

        if pos is None:
            open_pos(side, float(price))
        elif pos["side"] == side:
            if len(pos["lots"]) < cap:
                note = lot_notional()
                qty = note / float(price)
                eq -= note * fee
                set_pnl -= note * fee
                pos["lots"].append((float(price), qty))
        else:
            close_pos(float(price), t_i)
            open_pos(side, float(price))

    unreal = 0.0
    pos_side = "-"
    pos_lots = 0
    pos_avg = 0.0
    lot_entries: List[float] = []
    if pos is not None:
        last = float(close[-1])
        total_qty = 0.0
        total_cost = 0.0
        for (p, q) in pos["lots"]:
            unreal += q * (last - p) if pos["side"] == "long" else q * (p - last)
            total_qty += q
            total_cost += p * q
            lot_entries.append(round(p, 6))
        pos_side = pos["side"]
        pos_lots = len(pos["lots"])
        pos_avg = round(total_cost / total_qty, 6) if total_qty else 0.0

    wins = sum(1 for s in sets_pct if s > 0)
    losses = sum(1 for s in sets_pct if s < 0)
    closed = wins + losses
    winrate = (wins / closed * 100.0) if closed > 0 else None
    total_eq = eq + unreal
    expectancy = (sum(sets_pct) / len(sets_pct)) if sets_pct else 0.0

    return dict(
        signals=len(signals),
        sets=len(sets_pct),
        wins=wins,
        losses=losses,
        winrate=winrate,
        pnl_pct=(total_eq - cfg["capital"]) / cfg["capital"] * 100.0,
        realized_pct=(eq - cfg["capital"]) / cfg["capital"] * 100.0,
        unreal_pct=unreal / cfg["capital"] * 100.0,
        pos_side=pos_side,
        pos_lots=pos_lots,
        pos_avg=pos_avg,
        lot_entries=lot_entries,
        sets_tpnl=[[int(t), round(p, 2)] for t, p in zip(sets_ts, sets_pct)],
        last_side=(signals[-1][1] if signals else "-"),
        max_dd=round(max_dd, 2),
        expectancy=round(expectancy, 3),
    )


def top_symbols(n: int) -> List[Tuple[str, float]]:
    r = requests.get(f"{BINANCE_FAPI}/fapi/v1/ticker/24hr", timeout=30)
    r.raise_for_status()
    data = [d for d in r.json() if str(d.get("symbol", "")).endswith("USDT")]
    data.sort(key=lambda d: float(d.get("quoteVolume", 0)), reverse=True)
    return [(d["symbol"], float(d.get("quoteVolume", 0))) for d in data[:n]]


def fetch_klines(symbol: str, interval: str, days: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    end = int(time.time() * 1000)
    start = end - days * 24 * 60 * 60 * 1000
    out = []
    cur = start
    while cur < end:
        r = requests.get(
            f"{BINANCE_FAPI}/fapi/v1/klines",
            params=dict(symbol=symbol, interval=interval, startTime=cur, limit=1500),
            timeout=30,
        )
        r.raise_for_status()
        chunk = r.json()
        if not chunk:
            break
        out.extend(chunk)
        cur = int(chunk[-1][0]) + 1
        if len(chunk) < 1500:
            break
        time.sleep(0.25)

    high = np.array([float(k[2]) for k in out], dtype=float)
    low = np.array([float(k[3]) for k in out], dtype=float)
    close = np.array([float(k[4]) for k in out], dtype=float)
    ts = np.array([int(k[0]) // 1000 for k in out], dtype=np.int64)
    return high, low, close, ts


def simulate_portfolio(coins: Sequence[Dict], cfg: Dict) -> Dict:
    events = []
    for coin in coins:
        for i, side in coin["signals"]:
            events.append((int(coin["times"][i]), coin["symbol"], side, float(coin["close"][i])))
    events.sort(key=lambda e: e[0])

    d0 = cfg["portfolio_deposit"]
    eq = d0
    risk = cfg["port_risk_pct"] / 100.0
    fee = cfg["fee_per_side"]
    cap = cfg["max_lots"]
    pos: Dict[str, Dict] = {}
    peak_eq = eq
    max_dd = 0.0
    peak_conc = 0
    peak_expo = 0.0

    def expo_pct() -> float:
        notional = 0.0
        for p in pos.values():
            for pp, qq in p["lots"]:
                notional += pp * qq
        return notional / eq * 100.0 if eq > 0 else 0.0

    for _, sym, side, price in events:
        note = risk * eq
        p = pos.get(sym)
        if p is None:
            qty = note / price
            eq -= note * fee
            pos[sym] = {"side": side, "lots": [(price, qty)]}
        elif p["side"] == side:
            if len(p["lots"]) < cap:
                qty = note / price
                eq -= note * fee
                p["lots"].append((price, qty))
        else:
            realized = 0.0
            close_note = 0.0
            for pp, qq in p["lots"]:
                realized += qq * (price - pp) if p["side"] == "long" else qq * (pp - price)
                close_note += qq * price
            eq += realized - close_note * fee
            qty = note / price
            eq -= note * fee
            pos[sym] = {"side": side, "lots": [(price, qty)]}

        peak_eq = max(peak_eq, eq)
        if peak_eq > 0:
            max_dd = max(max_dd, (peak_eq - eq) / peak_eq * 100.0)
        peak_conc = max(peak_conc, sum(1 for p in pos.values() if p["lots"]))
        peak_expo = max(peak_expo, expo_pct())

    return dict(
        final_pct=(eq - d0) / d0 * 100.0,
        max_dd=max_dd,
        peak_positions=peak_conc,
        peak_exposure=peak_expo,
        n_signals=len(events),
    )


def portfolio_rows(pf: Dict, cfg: Dict, updated: str, coin_count: int) -> List[List]:
    return [
        PF_HEADERS,
        ["updated", updated],
        ["deposit", cfg["portfolio_deposit"]],
        ["entry_pct_per_signal", cfg["port_risk_pct"]],
        ["coins", coin_count],
        ["signals", pf["n_signals"]],
        ["final_pct", round(pf["final_pct"], 2)],
        ["max_dd", round(pf["max_dd"], 2)],
        ["peak_positions", pf["peak_positions"]],
        ["peak_exposure", round(pf["peak_exposure"], 2)],
    ]


def build_tabs() -> Dict[str, List[List]]:
    updated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if CFG["symbols"]:
        symbols = [((s if s.endswith("USDT") else s + "USDT"), 0.0) for s in CFG["symbols"]]
    else:
        symbols = top_symbols(CFG["top_n"])

    coins = []
    print(f"Loading {len(symbols)} symbols, interval={CFG['interval']}, days={CFG['history_days']}")
    for idx, (symbol, qvol) in enumerate(symbols, 1):
        try:
            high, low, close, times = fetch_klines(symbol, CFG["interval"], CFG["history_days"])
            if len(close) < IND["len"] + 50:
                print(f"[{idx}/{len(symbols)}] {symbol}: not enough candles, skipped")
                continue
            signals = compute_signals(close, high, low, CFG["tf_min"])
            coins.append(dict(symbol=symbol, qvol=qvol, high=high, low=low, close=close, times=times, signals=signals))
            print(f"[{idx}/{len(symbols)}] {symbol}: candles={len(close)} signals={len(signals)}")
        except Exception as exc:
            print(f"[{idx}/{len(symbols)}] {symbol}: ERROR {exc}", file=sys.stderr)

    tabs: Dict[str, List[List]] = {}
    for preset in PRESETS.values():
        cfg = dict(CFG)
        cfg.update(preset["rules"])
        rows: List[List] = []
        pf_coins = []
        for coin in coins:
            res = simulate(coin["close"], coin["signals"], cfg, times=coin["times"])
            if res["sets"] < cfg["write_min_sets"]:
                continue
            rows.append([
                coin["symbol"],
                cfg["interval"],
                res["signals"],
                res["sets"],
                res["wins"],
                res["losses"],
                "" if res["winrate"] is None else round(res["winrate"], 1),
                round(res["pnl_pct"], 2),
                round(res["realized_pct"], 2),
                round(res["unreal_pct"], 2),
                res["pos_side"],
                res["pos_lots"],
                res["pos_avg"],
                res["last_side"],
                round(coin["qvol"]),
                updated,
                json.dumps(res["lot_entries"], separators=(",", ":")),
                json.dumps(res["sets_tpnl"], separators=(",", ":")),
                res["max_dd"],
                res["expectancy"],
            ])
            pf_coins.append(coin)

        rows.sort(key=lambda r: r[7], reverse=True)
        tabs[preset["tab"]] = [HEADERS] + rows
        tabs[preset["pf_tab"]] = portfolio_rows(simulate_portfolio(pf_coins, cfg), cfg, updated, len(pf_coins))
        print(f"Prepared {preset['tab']}: {len(rows)} rows")

    return tabs


def post_tabs(tabs: Dict[str, List[List]]) -> None:
    webhook_url = os.getenv("SHEET_WEBHOOK_URL", "").strip()
    secret = os.getenv("SHEET_SECRET", "").strip()
    if not webhook_url:
        raise SystemExit("SHEET_WEBHOOK_URL is not set")
    if not secret:
        raise SystemExit("SHEET_SECRET is not set")

    payload = {"secret": secret, "tabs": tabs}
    r = requests.post(webhook_url, json=payload, timeout=120)
    print(f"Webhook status: {r.status_code}")
    print(r.text[:1000])
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        data = {"ok": r.text.strip() == "ok"}
    if not data.get("ok"):
        raise RuntimeError(f"Webhook returned error: {data}")


def main() -> None:
    tabs = build_tabs()
    post_tabs(tabs)
    print("Done")


if __name__ == "__main__":
    main()
