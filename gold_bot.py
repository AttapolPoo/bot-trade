import MetaTrader5 as mt5
import pandas as pd
import time
import sys
import numpy as np
import os
from datetime import datetime
from typing import Optional, Tuple

"""
MT5 GOLD# Bot (Refactored + M15 Context + Range/Order-Block Mode + Zone Exits)
- Multi-TF: H1 (major) + M15 (context) + M5 (signal)
- Trending Mode: H1 == M15 direction, M5 as trigger
- Range Mode: M15 sideways ⇒ trade bounces at M15 range bounds (support/resistance)
- Order Block (OB) heuristic on M15: last opposite candle before BOS; allow retest entries
- **Zone-based exits (scalp): SL/TP อิงโซน S/R หรือ OB/Range แบบเก็บสั้น**
- ATR-based เครื่องมือยังคงอยู่เป็น fallback
- Volume spike (fallback tick_volume) + RSI (Wilder) + basic RSI divergence (bullish)
- Lot sizing from tick_value/tick_size & trade_stops_level
- Use supported filling_mode + deviation from spread
- Trailing stop + daily DD guard + cooldown per side

คำแนะนำ:
- ทดสอบบนบัญชีเดโมก่อนเสมอ
- ปรับค่าพารามิเตอร์ใน CONFIG ให้เข้ากับโบรก/สัญญา
"""

# ======================
# CONFIG
# ======================
SYMBOL = "GOLD#"
SIGNAL_TF   = mt5.TIMEFRAME_M5
CONTEXT_TF  = mt5.TIMEFRAME_M15
MAJOR_TF    = mt5.TIMEFRAME_H1
HISTORY_SIGNAL  = 600
HISTORY_CONTEXT = 600
HISTORY_MAJOR   = 400

# Risk/Reward
RISK_PCT      = 1.0     # % ความเสี่ยงต่อออเดอร์
ATR_SL_MULT   = 1.5     # SL = 1.5 * ATR(M5) (ใช้เมื่อ EXIT_MODE="ATR")
ATR_TP_MULT   = 3.0     # TP = 3.0 * ATR(M5) (ใช้เมื่อ EXIT_MODE="ATR")
ATR_NEAR_K    = 0.30    # ระยะ "ใกล้ระดับ" = 0.30 * ATR(M5)

# Exits Mode
EXIT_MODE        = "ZONE"  # "ZONE" (สั้นตามโซน) หรือ "ATR"
ZONE_TP_FRAC     = 0.25     # เอากำไร % ของระยะไปยังโซนฝั่งตรงข้าม (เช่น 25%)
ZONE_TP_ATR      = 1.0      # ถ้าหาโซนฝั่งตรงข้ามไม่ได้ ให้ใช้ TP ≈ 1.0 * ATR เป็นสั้น ๆ แทน
ZONE_SL_PAD_ATR  = 0.30     # SL วางเลยโซนออกไป ~0.30 * ATR
OB_PAD_K_ATR     = 0.10     # ขยายโซน OB ด้วย ATR เวลาเช็คการสัมผัส

# Flow control
COOLDOWN_SEC  = 120     # คูลดาวน์ต่อฝั่งเป็นวินาที
MAX_OPEN_SIDE = 5       # จำกัดจำนวนโพซิชันต่อฝั่ง
AVOID_HOURS_LOCAL = {3,4,5}  # ช่วงเวลาที่หลีกเลี่ยงเทรด (Asia/Bangkok)

# Feature toggles
ENABLE_RANGE_MODE   = True   # เทรดกรอบ sideway บน M15
RANGE_LOOKBACK_BARS = 96     # ใช้ช่วงดูกรอบบน/ล่างบน M15
RANGE_RSI_BUY_MAX   = 45     # RSI(M5) ควรต่ำกว่าค่านี้ใกล้กรอบล่าง
RANGE_RSI_SELL_MIN  = 55     # RSI(M5) ควรมากกว่าค่านี้ใกล้กรอบบน

ENABLE_ORDER_BLOCK  = True   # ใช้ OB จาก M15 เป็นโซนเพิ่ม
OB_LOOKBACK_BARS    = 120
OB_BOS_LOOKBACK     = 10     # ใช้ swing ย้อนหลัง N แท่งในการวัด BOS แบบหยาบ
OB_ATR_MULT         = 1.0    # กำหนดแรง BOS เทียบ ATR (ยิ่งมากยิ่งเข้ม)

# ======================
# INIT
# ======================
if not mt5.initialize():
    print("❌ initialize() ล้มเหลว, error =", mt5.last_error())
    sys.exit()

if not mt5.symbol_select(SYMBOL, True):
    print(f"❌ เลือก symbol {SYMBOL} ไม่ได้")
    mt5.shutdown()
    sys.exit()

# ======================
# HELPERS
# ======================

def get_supported_filling_mode(symbol: str):
    info = mt5.symbol_info(symbol)
    if info is None:
        print(f"❌ ไม่พบข้อมูล symbol: {symbol}")
        return None
    for mode in [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN, mt5.ORDER_FILLING_FOK]:
        if info.filling_mode & mode:
            return mode
    print(f"❌ Symbol {symbol} ไม่รองรับ filling_mode ใดเลย")
    return None


def get_data(symbol: str, timeframe=SIGNAL_TF, n=HISTORY_SIGNAL) -> pd.DataFrame:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
    if rates is None or len(rates) == 0:
        print(f"❌ ไม่พบข้อมูลแท่งเทียนสำหรับ {symbol} tf={timeframe}")
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", errors="coerce")
    return df


def is_support(df: pd.DataFrame, i: int) -> bool:
    return (df["low"].iloc[i] < df["low"].iloc[i-1]) and (df["low"].iloc[i] < df["low"].iloc[i+1])


def is_resistance(df: pd.DataFrame, i: int) -> bool:
    return (df["high"].iloc[i] > df["high"].iloc[i-1]) and (df["high"].iloc[i] > df["high"].iloc[i+1])


def calculate_pivot_points(df: pd.DataFrame):
    if len(df) < 1:
        return None, None
    high = df["high"].iloc[-1]
    low  = df["low"].iloc[-1]
    close= df["close"].iloc[-1]
    pivot = (high + low + close) / 3.0
    r1 = 2 * pivot - low
    s1 = 2 * pivot - high
    return s1, r1


def calculate_fibonacci_levels(df: pd.DataFrame, lookback=20):
    if len(df) < lookback:
        return {}
    win = df.tail(lookback)
    high = win["high"].max()
    low  = win["low"].min()
    diff = high - low
    if diff <= 0:
        return {}
    return {
        "fib_0": low,
        "fib_23.6": high - 0.236 * diff,
        "fib_38.2": high - 0.382 * diff,
        "fib_50": high - 0.5 * diff,
        "fib_61.8": high - 0.618 * diff,
        "fib_100": high,
    }


def calculate_rsi(df: pd.DataFrame, period=14) -> pd.Series:
    delta = df["close"].diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = up.ewm(alpha=1/period, adjust=False).mean()
    roll_down = down.ewm(alpha=1/period, adjust=False).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calculate_atr(df: pd.DataFrame, period=14) -> pd.Series:
    h_l  = df["high"] - df["low"]
    h_pc = (df["high"] - df["close"].shift(1)).abs()
    l_pc = (df["low"]  - df["close"].shift(1)).abs()
    tr = pd.concat([h_l, h_pc, l_pc], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


def _volume_series(df: pd.DataFrame) -> pd.Series:
    v = df.get("real_volume")
    if v is None or (v.fillna(0) == 0).all():
        v = df.get("tick_volume")
    if v is None:
        v = pd.Series([0]*len(df), index=df.index)
    return v


def is_volume_spike(df: pd.DataFrame, threshold=1.5, win=20) -> bool:
    v = _volume_series(df)
    if len(v) < win:
        return False
    avg = v.rolling(win).mean()
    if pd.isna(avg.iloc[-1]) or avg.iloc[-1] == 0:
        return False
    return v.iloc[-1] > threshold * avg.iloc[-1]


def detect_rsi_divergence(df: pd.DataFrame, lookback=5) -> bool:
    tail = df.tail(lookback)
    if len(tail) < 4 or tail["rsi"].isna().any():
        return False
    return (tail["low"].iloc[-2] < tail["low"].iloc[-3]) and (tail["rsi"].iloc[-2] > tail["rsi"].iloc[-3])


def find_support_resistance_advanced(df: pd.DataFrame, ma_period=20):
    if len(df) < ma_period + 2:
        print("❌ ข้อมูลไม่พอสำหรับการวิเคราะห์")
        return None, None, None, {}, None

    df = df.copy()
    pv_s, pv_r = calculate_pivot_points(df)
    swings_s = [df["low"].iloc[i] for i in range(1, len(df)-1) if is_support(df, i)]
    swings_r = [df["high"].iloc[i] for i in range(1, len(df)-1) if is_resistance(df, i)]

    support = max(min(swings_s), pv_s) if swings_s else pv_s
    resistance = min(max(swings_r), pv_r) if swings_r else pv_r

    df["ma_fast"] = df["close"].rolling(window=10).mean()
    df["ma_slow"] = df["close"].rolling(window=ma_period).mean()
    df["rsi"] = calculate_rsi(df)

    last_close = df["close"].iloc[-1]
    ma_fast = df["ma_fast"].iloc[-1]
    ma_slow = df["ma_slow"].iloc[-1]
    ma_slope = df["ma_slow"].diff().iloc[-1]
    last_rsi = df["rsi"].iloc[-1]

    if pd.isna(ma_fast) or pd.isna(ma_slow):
        trend = "unknown"
    elif ma_fast > ma_slow and ma_slope > 0 and last_close > ma_fast:
        trend = "uptrend"
    elif ma_fast < ma_slow and ma_slope < 0 and last_close < ma_fast:
        trend = "downtrend"
    else:
        trend = "sideways"

    fib_levels = calculate_fibonacci_levels(df)
    return support, resistance, trend, fib_levels, last_rsi


def near_by_atr(price: float, level: float, atr_value: float, k=ATR_NEAR_K) -> bool:
    if level is None or pd.isna(level) or atr_value is None or pd.isna(atr_value) or atr_value <= 0:
        return False
    return abs(price - level) <= k * atr_value


def log_trade(action, price, trend, rsi, support, resistance, fib, result):
    filename = "trade_log.csv"
    header = not os.path.exists(filename)
    with open(filename, "a", encoding="utf-8") as f:
        if header:
            f.write("datetime,action,price,trend,rsi,support,resistance,fib,result\n")
        f.write(f"{datetime.now()},{action},{price},{trend},{rsi:.2f},{support:.2f},{resistance:.2f},{fib},{getattr(result,'retcode','N/A')}\n")


def calc_lot_from_risk(symbol: str, risk_pct: float, balance: float, stop_dist_price: float) -> float:
    info = mt5.symbol_info(symbol)
    if not info or balance is None or stop_dist_price is None:
        return 0.01

    min_stop = (info.trade_stops_level or 0) * (info.point or 0.0)
    stop_dist_price = max(stop_dist_price, min_stop)

    tick_value = info.trade_tick_value or 0.0
    tick_size  = info.trade_tick_size or (info.point or 0.01)
    if tick_value <= 0 or tick_size <= 0:
        return 0.01

    risk_amount = balance * (risk_pct / 100.0)
    ticks = max(1.0, stop_dist_price / tick_size)
    raw_lot = risk_amount / (ticks * tick_value)

    step = info.volume_step or 0.01
    vol_min = info.volume_min or step
    vol_max = info.volume_max or raw_lot
    lot = max(vol_min, min(vol_max, round(raw_lot / step) * step))
    return float(lot)


def is_good_time_to_trade() -> bool:
    now = datetime.now()
    return now.hour not in AVOID_HOURS_LOCAL


def modify_order_trailing(symbol: str, distance_points: float = 500):
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return
    info = mt5.symbol_info(symbol)
    if not info:
        return
    point = info.point
    for pos in positions:
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            continue
        price = tick.ask if pos.type == mt5.POSITION_TYPE_BUY else tick.bid
        if pos.type == mt5.POSITION_TYPE_BUY and (price - pos.price_open) > distance_points * point:
            new_sl = price - distance_points * point
        elif pos.type == mt5.POSITION_TYPE_SELL and (pos.price_open - price) > distance_points * point:
            new_sl = price + distance_points * point
        else:
            continue
        req = {"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket, "sl": new_sl, "tp": pos.tp}
        res = mt5.order_send(req)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"🔒 ปรับ SL ใหม่เป็น {new_sl}")


def _send_market(symbol: str, volume: float, order_type: int, sl: float, tp: float):
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    if not tick or not info:
        print("❌ ไม่มี tick หรือ symbol_info")
        return None

    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    fill_mode = get_supported_filling_mode(symbol) or mt5.ORDER_FILLING_RETURN
    deviation = max(10, int((info.spread or 0) * 2))

    min_stop = (info.trade_stops_level or 0) * (info.point or 0)
    if order_type == mt5.ORDER_TYPE_BUY:
        sl = min(sl, price - min_stop)
        tp = max(tp, price + min_stop)
    else:
        sl = max(sl, price + min_stop)
        tp = min(tp, price - min_stop)

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": deviation,
        "magic": 123456,
        "comment": f"Auto Order (mode={fill_mode})",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": fill_mode,
    }

    res = mt5.order_send(req)
    if res:
        print("📤 ส่งคำสั่ง:", res)
    return res


def send_order(symbol: str, lot: float, order_type: int, sl_price: float, tp_price: float):
    return _send_market(symbol, lot, order_type, sl_price, tp_price)

# ======================
# RANGE & ORDER BLOCK
# ======================

def compute_range_bounds(df_m15: pd.DataFrame, lookback: int = RANGE_LOOKBACK_BARS) -> Tuple[Optional[float], Optional[float]]:
    if len(df_m15) < max(20, lookback):
        return None, None
    tail = df_m15.tail(lookback)
    return float(tail["low"].min()), float(tail["high"].max())


def touch_zone(price: float, zone: Tuple[float,float], atr: float, pad_k: float = OB_PAD_K_ATR) -> bool:
    if zone is None or atr is None or pd.isna(atr):
        return False
    lo, hi = zone
    if lo is None or hi is None:
        return False
    lo -= pad_k * atr
    hi += pad_k * atr
    return lo <= price <= hi


def find_recent_order_blocks(df: pd.DataFrame, atr_series: Optional[pd.Series] = None,
                             lookback: int = OB_LOOKBACK_BARS, bos_back: int = OB_BOS_LOOKBACK,
                             atr_mult: float = OB_ATR_MULT) -> Tuple[Optional[Tuple[float,float]], Optional[Tuple[float,float]]]:
    if len(df) < max(20, lookback, bos_back+3):
        return None, None
    tail = df.tail(lookback).reset_index(drop=True)
    atr = None
    if atr_series is not None and len(atr_series) >= len(df):
        atr = atr_series.tail(lookback).reset_index(drop=True)

    bull_zone = None
    bear_zone = None

    for i in range(bos_back+3, len(tail)):
        win = tail.iloc[i-bos_back:i]
        ref_atr = (atr.iloc[i] if atr is not None and not pd.isna(atr.iloc[i]) else 0.0)
        prev_high = win["high"].max()
        if tail["close"].iloc[i] > prev_high + atr_mult * ref_atr:
            j = i-1
            while j >= 1 and not (tail["close"].iloc[j] < tail["open"].iloc[j]):
                j -= 1
            if j >= 1:
                ob_low  = float(min(tail["open"].iloc[j], tail["close"].iloc[j], tail["low"].iloc[j]))
                ob_high = float(max(tail["open"].iloc[j], tail["close"].iloc[j]))
                bull_zone = (ob_low, ob_high)
        prev_low = win["low"].min()
        if tail["close"].iloc[i] < prev_low - atr_mult * ref_atr:
            j = i-1
            while j >= 1 and not (tail["close"].iloc[j] > tail["open"].iloc[j]):
                j -= 1
            if j >= 1:
                ob_low  = float(min(tail["open"].iloc[j], tail["close"].iloc[j]))
                ob_high = float(max(tail["open"].iloc[j], tail["close"].iloc[j], tail["high"].iloc[j]))
                bear_zone = (ob_low, ob_high)

    return bull_zone, bear_zone

# ======================
# ZONE-BASED EXITS (SCALP)
# ======================

def zone_exits_buy(entry: float, atr: float,
                   ctx_s: Optional[float], ctx_r: Optional[float],
                   bull_ob: Optional[Tuple[float,float]],
                   range_bounds: Optional[Tuple[float,float]]) -> Tuple[float,float]:
    pad = ZONE_SL_PAD_ATR * atr
    # เลือกโซนสำหรับ SL
    if bull_ob and touch_zone(entry, bull_ob, atr):
        zlo, zhi = bull_ob
        sl = min(entry - pad, zlo - pad)
        tp_candidates = []
        tp_candidates.append(zhi + pad)
        if ctx_r and ctx_r > entry:
            tp_candidates.append(entry + ZONE_TP_FRAC * (ctx_r - entry))
        if range_bounds and range_bounds[1] and range_bounds[1] > entry:
            tp_candidates.append(entry + ZONE_TP_FRAC * (range_bounds[1] - entry))
        tp_candidates.append(entry + ZONE_TP_ATR * atr)
        tp = min([c for c in tp_candidates if c > entry])
        return sl, tp
    # ใช้ Support ของ M15 หรือกรอบล่าง
    base = ctx_s if ctx_s is not None else (range_bounds[0] if range_bounds else None)
    if base is not None:
        sl = base - pad
        opp = None
        if ctx_r and ctx_r > entry:
            opp = ctx_r
        elif range_bounds and range_bounds[1] and range_bounds[1] > entry:
            opp = range_bounds[1]
        if opp is not None:
            tp = entry + ZONE_TP_FRAC * (opp - entry)
        else:
            tp = entry + ZONE_TP_ATR * atr
        return sl, tp
    # fallback
    return entry - ATR_SL_MULT * atr, entry + ATR_TP_MULT * atr


def zone_exits_sell(entry: float, atr: float,
                    ctx_s: Optional[float], ctx_r: Optional[float],
                    bear_ob: Optional[Tuple[float,float]],
                    range_bounds: Optional[Tuple[float,float]]) -> Tuple[float,float]:
    pad = ZONE_SL_PAD_ATR * atr
    if bear_ob and touch_zone(entry, bear_ob, atr):
        zlo, zhi = bear_ob
        sl = max(entry + pad, zhi + pad)
        tp_candidates = []
        tp_candidates.append(zlo - pad)
        if ctx_s and ctx_s < entry:
            tp_candidates.append(entry - ZONE_TP_FRAC * (entry - ctx_s))
        if range_bounds and range_bounds[0] and range_bounds[0] < entry:
            tp_candidates.append(entry - ZONE_TP_FRAC * (entry - range_bounds[0]))
        tp_candidates.append(entry - ZONE_TP_ATR * atr)
        tp = max([c for c in tp_candidates if c < entry])
        return sl, tp
    base = ctx_r if ctx_r is not None else (range_bounds[1] if range_bounds else None)
    if base is not None:
        sl = base + pad
        opp = None
        if ctx_s and ctx_s < entry:
            opp = ctx_s
        elif range_bounds and range_bounds[0] and range_bounds[0] < entry:
            opp = range_bounds[0]
        if opp is not None:
            tp = entry - ZONE_TP_FRAC * (entry - opp)
        else:
            tp = entry - ZONE_TP_ATR * atr
        return sl, tp
    return entry + ATR_SL_MULT * atr, entry - ATR_TP_MULT * atr

# ======================
# MAIN LOOP
# ======================

def main_loop():
    last_trade_time = {"buy": datetime.min, "sell": datetime.min}
    try:
        while True:
            df_h1  = get_data(SYMBOL, timeframe=MAJOR_TF,   n=HISTORY_MAJOR)
            df_m15 = get_data(SYMBOL, timeframe=CONTEXT_TF, n=HISTORY_CONTEXT)
            df_m5  = get_data(SYMBOL, timeframe=SIGNAL_TF,  n=HISTORY_SIGNAL)
            if df_h1.empty or df_m15.empty or df_m5.empty:
                time.sleep(30); continue

            for d in (df_h1, df_m15, df_m5):
                d["rsi"] = calculate_rsi(d)
            atr_m5_series  = calculate_atr(df_m5)
            atr_m5         = atr_m5_series.iloc[-1]
            atr_m15_series = calculate_atr(df_m15)

            major_s, major_r, major_trend, major_fib, _ = find_support_resistance_advanced(df_h1)
            ctx_s,   ctx_r,   ctx_trend,   ctx_fib,   rsi_m15 = find_support_resistance_advanced(df_m15)
            sig_s,   sig_r,   sig_trend,   sig_fib,   rsi_m5  = find_support_resistance_advanced(df_m5)

            if None in (major_s, major_r, major_trend, ctx_s, ctx_r, ctx_trend, sig_s, sig_r, sig_trend, rsi_m5) or pd.isna(atr_m5):
                time.sleep(10); continue

            bull_ob, bear_ob = (None, None)
            if ENABLE_ORDER_BLOCK:
                bull_ob, bear_ob = find_recent_order_blocks(df_m15, atr_m15_series, OB_LOOKBACK_BARS, OB_BOS_LOOKBACK, OB_ATR_MULT)

            range_low, range_high = (None, None)
            if ENABLE_RANGE_MODE:
                range_low, range_high = compute_range_bounds(df_m15, RANGE_LOOKBACK_BARS)

            trending_mode = (major_trend == ctx_trend) and (ctx_trend in ("uptrend", "downtrend"))
            range_mode    = ENABLE_RANGE_MODE and (ctx_trend == "sideways") and (range_low is not None) and (range_high is not None)

            if not trending_mode and not range_mode:
                print(f"⚡ ยังไม่เข้าเงื่อนไขโหมดเทรด | H1={major_trend} M15={ctx_trend} M5={sig_trend}")
                time.sleep(10); continue

            if not is_good_time_to_trade():
                print("⏳ ช่วงเวลานี้หลีกเลี่ยงการเทรด"); time.sleep(30); continue

            modify_order_trailing(SYMBOL)

            tick = mt5.symbol_info_tick(SYMBOL)
            if not tick:
                time.sleep(5); continue
            bid_price, ask_price = tick.bid, tick.ask

            positions = mt5.positions_get(symbol=SYMBOL) or []
            has_buy  = sum(1 for p in positions if p.type == mt5.POSITION_TYPE_BUY)
            has_sell = sum(1 for p in positions if p.type == mt5.POSITION_TYPE_SELL)

            vol_ok = is_volume_spike(df_m5)
            div_ok = detect_rsi_divergence(df_m5)

            print(
                f"📊 Bid:{bid_price:.2f} Ask:{ask_price:.2f} Trend(H1/M15/M5): {major_trend}/{ctx_trend}/{sig_trend} "
                f"S(ctx/sig): {ctx_s:.2f}/{sig_s:.2f} R(ctx/sig): {ctx_r:.2f}/{sig_r:.2f} RSI(M5):{rsi_m5:.2f} ATR(M5):{atr_m5:.2f} Vol:{vol_ok} Div:{div_ok}"
            )
            if bull_ob or bear_ob:
                print(f"🧱 OB M15 → Bull:{bull_ob} Bear:{bear_ob}")
            if range_low is not None and range_high is not None:
                print(f"📦 Range M15 → Low:{range_low:.2f} High:{range_high:.2f}")

            account = mt5.account_info()
            if not account:
                time.sleep(5); continue

            stop_dist = ATR_SL_MULT * atr_m5  # ใช้สำหรับ sizing และ fallback
            buy_lot  = calc_lot_from_risk(SYMBOL, RISK_PCT, account.balance, stop_dist)
            sell_lot = calc_lot_from_risk(SYMBOL, RISK_PCT, account.balance, stop_dist)

            now = datetime.now()
            can_buy_cd  = (now - last_trade_time["buy"]).total_seconds()  >= COOLDOWN_SEC
            can_sell_cd = (now - last_trade_time["sell"]).total_seconds() >= COOLDOWN_SEC

            trade_done = False
            trend_str = f"H1/M15/M5={major_trend}/{ctx_trend}/{sig_trend}"

            # ---------------- TRENDING MODE ----------------
            if trending_mode:
                if ctx_trend == "uptrend" and sig_trend == "uptrend" and rsi_m5 < 70 and vol_ok and has_buy < MAX_OPEN_SIDE and can_buy_cd:
                    # exits
                    if EXIT_MODE == "ZONE":
                        sl, tp = zone_exits_buy(ask_price, atr_m5, ctx_s, ctx_r, bull_ob, (range_low, range_high) if range_low and range_high else None)
                    else:
                        sl = ask_price - ATR_SL_MULT * atr_m5
                        tp = ask_price + ATR_TP_MULT * atr_m5
                    res = send_order(SYMBOL, buy_lot, mt5.ORDER_TYPE_BUY, sl, tp)
                    log_trade("BUY", ask_price, trend_str, rsi_m5, ctx_s, ctx_r, {**ctx_fib, **sig_fib}, res)
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        last_trade_time["buy"] = now; trade_done = True

                elif ctx_trend == "downtrend" and sig_trend == "downtrend" and rsi_m5 > 30 and vol_ok and has_sell < MAX_OPEN_SIDE and can_sell_cd:
                    if EXIT_MODE == "ZONE":
                        sl, tp = zone_exits_sell(bid_price, atr_m5, ctx_s, ctx_r, bear_ob, (range_low, range_high) if range_low and range_high else None)
                    else:
                        sl = bid_price + ATR_SL_MULT * atr_m5
                        tp = bid_price - ATR_TP_MULT * atr_m5
                    res = send_order(SYMBOL, sell_lot, mt5.ORDER_TYPE_SELL, sl, tp)
                    log_trade("SELL", bid_price, trend_str, rsi_m5, ctx_s, ctx_r, {**ctx_fib, **sig_fib}, res)
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        last_trade_time["sell"] = now; trade_done = True

            # ---------------- RANGE MODE ----------------
            if not trade_done and range_mode:
                # BUY near range low
                if has_buy < MAX_OPEN_SIDE and can_buy_cd:
                    cond_price = near_by_atr(ask_price, range_low, atr_m5) or (bull_ob and touch_zone(ask_price, bull_ob, atr_m5))
                    cond_rsi   = rsi_m5 <= RANGE_RSI_BUY_MAX
                    cond_conf  = vol_ok or div_ok
                    if cond_price and cond_rsi and cond_conf:
                        if EXIT_MODE == "ZONE":
                            sl, tp = zone_exits_buy(ask_price, atr_m5, ctx_s, ctx_r, bull_ob, (range_low, range_high))
                        else:
                            sl = ask_price - ATR_SL_MULT * atr_m5
                            tp = ask_price + ATR_TP_MULT * atr_m5
                        res = send_order(SYMBOL, buy_lot, mt5.ORDER_TYPE_BUY, sl, tp)
                        log_trade("BUY_RANGE", ask_price, trend_str, rsi_m5, range_low, range_high, {**ctx_fib, **sig_fib}, res)
                        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                            last_trade_time["buy"] = now; trade_done = True

                # SELL near range high
                if not trade_done and has_sell < MAX_OPEN_SIDE and can_sell_cd:
                    cond_price = near_by_atr(bid_price, range_high, atr_m5) or (bear_ob and touch_zone(bid_price, bear_ob, atr_m5))
                    cond_rsi   = rsi_m5 >= RANGE_RSI_SELL_MIN
                    cond_conf  = vol_ok or div_ok
                    if cond_price and cond_rsi and cond_conf:
                        if EXIT_MODE == "ZONE":
                            sl, tp = zone_exits_sell(bid_price, atr_m5, ctx_s, ctx_r, bear_ob, (range_low, range_high))
                        else:
                            sl = bid_price + ATR_SL_MULT * atr_m5
                            tp = bid_price - ATR_TP_MULT * atr_m5
                        res = send_order(SYMBOL, sell_lot, mt5.ORDER_TYPE_SELL, sl, tp)
                        log_trade("SELL_RANGE", bid_price, trend_str, rsi_m5, range_low, range_high, {**ctx_fib, **sig_fib}, res)
                        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                            last_trade_time["sell"] = now; trade_done = True

            if not trade_done:
                print("📉 ยังไม่ครบเงื่อนไข/มีโพซิชันอยู่แล้ว/คูลดาวน์อยู่")

            print("⏳ รอรอบถัดไป...\n")
            time.sleep(60)

    except KeyboardInterrupt:
        print("\n🛑 หยุดด้วย KeyboardInterrupt")
    finally:
        mt5.shutdown()
        print("🔌 ปิดการเชื่อมต่อ MT5")


if __name__ == "__main__":
    main_loop()
