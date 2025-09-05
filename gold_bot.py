import MetaTrader5 as mt5
import pandas as pd
import time
import sys
import numpy as np
import os
from datetime import datetime, timedelta, date
from typing import Optional, Tuple, Dict

"""
MT5 GOLD# Bot (Pro Refactor)
- Multi-TF: H1 (major) + M15 (context) + M5 (signal)
- Trending Mode (follow trend) + Range Mode (bounce in box)
- Order Block (M15) heuristic for retest
- Zone-based exits (scalp): SL/TP อิง S/R / OB / Range (สั้น)
- ATR exits (fallback) + ATR-band proximity
- Volume spike + RSI (Wilder) + Divergence (bull/bear)
- Structure trigger (M5 BOS/CHoCH แบบง่าย)
- Entry modes: MARKET / LIMIT / STOP
- Risk mgmt: dynamic risk by drawdown, daily loss limit, max trades/day
- Filters: spread vs ATR, volatility regime, sessions, news no-trade windows
- Safety: respect trade_stops_level, supported filling_mode, deviation by spread
- QoL: trailing stop, cooldown per side, de-dup zone re-entry, logging

หมายเหตุ: ทดสอบเดโมก่อนใช้งานจริง ปรับ CONFIG ให้เหมาะกับสัญญา/โบรกของคุณ
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

# --- Risk/Reward & Exits ---
RISK_PCT_BASE   = 1.0      # ความเสี่ยงพื้นฐานต่อออเดอร์ (% ของ balance)
RISK_PCT_MIN    = 0.25     # ลดลงต่ำสุดเมื่อ DD สูง
ATR_SL_MULT     = 1.5      # SL = 1.5 * ATR(M5) (ใช้เมื่อ EXIT_MODE="ATR" หรือ fallback)
ATR_TP_MULT     = 3.0      # TP = 3.0 * ATR(M5)
ATR_NEAR_K      = 0.30     # ระยะ "ใกล้โซน" = 0.30 * ATR(M5)
EXIT_MODE       = "ZONE"   # "ZONE" (สั้นตามโซน) หรือ "ATR"
ZONE_TP_FRAC    = 0.25     # เอากำไร % ของระยะไปยังโซนฝั่งตรงข้าม
ZONE_TP_ATR     = 1.0      # ถ้าไม่มีโซนฝั่งตรงข้าม → ใช้ ATR(M5) * ค่า นี้
ZONE_SL_PAD_ATR = 0.30     # SL วางเลยโซนออกไป ~0.30 * ATR
OB_PAD_K_ATR    = 0.10     # ขยายโซน OB ตอนเช็คสัมผัส

# --- Entry control ---
ENTRY_MODE       = "MARKET"   # "MARKET" | "LIMIT" | "STOP"
ENTRY_OFFSET_ATR = 0.25        # ใช้กับ LIMIT/STOP: ระยะ offset จากราคา (หน่วย ATR)
COOLDOWN_SEC     = 120         # คูลดาวน์ต่อฝั่ง (วินาที)
MAX_OPEN_SIDE    = 5           # จำกัดจำนวนโพซิชันต่อฝั่ง
ZONE_REENTRY_COOLDOWN_MIN = 15 # ไม่ยิงซ้ำโซนเดิมภายใน x นาที

# --- Range & OB ---
ENABLE_RANGE_MODE   = True
RANGE_LOOKBACK_BARS = 96
RANGE_RSI_BUY_MAX   = 45
RANGE_RSI_SELL_MIN  = 55
ENABLE_ORDER_BLOCK  = True
OB_LOOKBACK_BARS    = 120
OB_BOS_LOOKBACK     = 10
OB_ATR_MULT         = 1.0

# --- Filters ---
AVOID_HOURS_LOCAL = {3,4,5}         # เลี่ยงบางชั่วโมง
ALLOWED_SESSIONS  = [(13, 23)]      # ช่วงชั่วโมงท้องถิ่นที่อนุญาตเทรด (ตัวอย่าง: 13:00-23:00)
SESSION_EDGE_MIN  = 5               # เลี่ยงนาทีแรก/ท้ายของสภาวะอนุญาต
SPREAD_ATR_MAX_RATIO = 0.15         # spread ไม่ควรเกิน % ของ ATR(M5)
VOL_FILTER_ENABLE = True
VOL_PCTL_MIN      = 20              # ATR(M5) ต้องอยู่เหนือ pctl นี้
VOL_PCTL_MAX      = 98              # และต่ำกว่า pctl นี้

# ข่าวแรง (ตั้งเองแบบแมนนวล)
NEWS_BUFFER_MIN   = 15              # ไม่เทรดภายใน +/- นาที รอบข่าว
NEWS_EVENTS_LOCAL = [
    # ("2025-09-10 19:30", "2025-09-10 19:35"),  # ตัวอย่าง: CPI
]

# --- Daily discipline ---
MAX_TRADES_PER_DAY = 10
DAILY_LOSS_PCT     = 3.0            # หยุดเทรดถ้า equity < start_of_day * (1-3%)
DRAWDOWN_PCT_SOFT  = 5.0            # ถ้า DD จาก HWM > 5% ลด risk ลงครึ่งหนึ่ง (ไม่ต่ำกว่า RISK_PCT_MIN)

# --- Scale-out & BE shift ---
SCALEOUT_ENABLED = True
TP1_R            = 0.5              # ปิดครึ่งหนึ่งเมื่อกำไรถึง 0.5R
BE_PAD_ATR       = 0.10             # เลื่อน SL → BE + pad ATR

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
# STATE (runtime)
# ======================
HWM_EQUITY: Optional[float] = None
DAY_START: Optional[date] = None
DAY_START_EQUITY: Optional[float] = None
TRADES_TODAY: int = 0
LAST_ZONE_TRADE_AT: Dict[str, datetime] = {}

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


def detect_rsi_divergence_bull(df: pd.DataFrame, lookback=5) -> bool:
    tail = df.tail(lookback)
    if len(tail) < 4 or tail["rsi"].isna().any():
        return False
    return (tail["low"].iloc[-2] < tail["low"].iloc[-3]) and (tail["rsi"].iloc[-2] > tail["rsi"].iloc[-3])


def detect_rsi_divergence_bear(df: pd.DataFrame, lookback=5) -> bool:
    tail = df.tail(lookback)
    if len(tail) < 4 or tail["rsi"].isna().any():
        return False
    return (tail["high"].iloc[-2] > tail["high"].iloc[-3]) and (tail["rsi"].iloc[-2] < tail["rsi"].iloc[-3])


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


def spread_ok(symbol: str, atr_m5: float, max_ratio: float = SPREAD_ATR_MAX_RATIO) -> bool:
    info = mt5.symbol_info(symbol)
    if not info or atr_m5 is None or pd.isna(atr_m5) or atr_m5 <= 0:
        return True
    spread_px = (info.spread or 0) * (info.point or 0)
    return spread_px <= max_ratio * atr_m5


def volatility_ok(atr_series: pd.Series, pmin: int = VOL_PCTL_MIN, pmax: int = VOL_PCTL_MAX) -> bool:
    if not VOL_FILTER_ENABLE:
        return True
    if atr_series is None or atr_series.dropna().empty:
        return True
    tail = atr_series.dropna().tail(300)
    if tail.empty:
        return True
    lo = np.nanpercentile(tail, pmin)
    hi = np.nanpercentile(tail, pmax)
    cur = tail.iloc[-1]
    return (cur >= lo) and (cur <= hi)


def in_allowed_sessions(now: datetime) -> bool:
    # อนุญาตเฉพาะช่วงชั่วโมงที่กำหนด และเลี่ยงนาทีแรก/ท้ายของช่วง
    for h0, h1 in ALLOWED_SESSIONS:
        if h0 <= now.hour < h1:
            if now.minute < SESSION_EDGE_MIN or (now.hour == h1-1 and now.minute >= 60-SESSION_EDGE_MIN):
                return False
            return True
    return False


def in_news_window(now: datetime) -> bool:
    # NEWS_EVENTS_LOCAL: list of ("YYYY-mm-dd HH:MM", "YYYY-mm-dd HH:MM") local time strings
    for s, e in NEWS_EVENTS_LOCAL:
        try:
            t0 = datetime.strptime(s, "%Y-%m-%d %H:%M") - timedelta(minutes=NEWS_BUFFER_MIN)
            t1 = datetime.strptime(e, "%Y-%m-%d %H:%M") + timedelta(minutes=NEWS_BUFFER_MIN)
            if t0 <= now <= t1:
                return True
        except Exception:
            continue
    return False


def update_daily_counters(equity: float):
    global DAY_START, DAY_START_EQUITY, TRADES_TODAY
    today = datetime.now().date()
    if DAY_START != today:
        DAY_START = today
        DAY_START_EQUITY = equity
        TRADES_TODAY = 0


# def daily_limits_ok(equity: float) -> bool:
#     if DAY_START_EQUITY is None:
#         return True
#     if TRADES_TODAY >= MAX_TRADES_PER_DAY:
#         print("⛔ เกินจำนวนเทรดต่อวัน")
#         return False
#     drop_pct = 100.0 * max(0.0, (DAY_START_EQUITY - equity)) / max(1e-9, DAY_START_EQUITY)
#     if drop_pct >= DAILY_LOSS_PCT:
#         print("🛑 Daily loss limit reached")
#         return False
#     return True


def dynamic_risk_pct(equity: float) -> float:
    global HWM_EQUITY
    if equity is None:
        return RISK_PCT_BASE
    if HWM_EQUITY is None:
        HWM_EQUITY = equity
    else:
        HWM_EQUITY = max(HWM_EQUITY, equity)
    dd_pct = 100.0 * max(0.0, (HWM_EQUITY - equity)) / max(1e-9, HWM_EQUITY)
    if dd_pct >= DRAWDOWN_PCT_SOFT:
        return max(RISK_PCT_MIN, RISK_PCT_BASE / 2.0)
    return RISK_PCT_BASE


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
    if now.hour in AVOID_HOURS_LOCAL:
        return False
    if not in_allowed_sessions(now):
        return False
    if in_news_window(now):
        return False
    return True


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

# -------- Structure Trigger (M5) --------

def m5_structure_trigger(df_m5: pd.DataFrame, lookback: int = 3) -> Tuple[bool, bool]:
    tail = df_m5.tail(lookback + 1)
    if len(tail) < lookback + 1:
        return False, False
    last_close = tail["close"].iloc[-1]
    prev_high = tail["high"].iloc[:-1].max()
    prev_low  = tail["low"].iloc[:-1].min()
    bos_up = last_close > prev_high
    bos_dn = last_close < prev_low
    return bos_up, bos_dn

# -------- Order Send (market & pending) --------

def _base_order_fields(symbol: str, order_type: int, price: float, sl: float, tp: float) -> dict:
    info = mt5.symbol_info(symbol)
    fill_mode = get_supported_filling_mode(symbol) or mt5.ORDER_FILLING_RETURN
    deviation = max(10, int((info.spread or 0) * 2)) if info else 10
    return {
        "symbol": symbol,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": deviation,
        "magic": 123456,
        "comment": f"Auto ({fill_mode})",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": fill_mode,
    }


def send_order_market(symbol: str, lot: float, side: str, sl: float, tp: float):
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    if not tick or not info:
        print("❌ ไม่มี tick หรือ symbol_info")
        return None
    price = tick.ask if side == "buy" else tick.bid
    # respect stops_level
    min_stop = (info.trade_stops_level or 0) * (info.point or 0)
    if side == "buy": sl, tp = min(sl, price - min_stop), max(tp, price + min_stop)
    else:              sl, tp = max(sl, price + min_stop), min(tp, price - min_stop)
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    req = {"action": mt5.TRADE_ACTION_DEAL, "volume": lot}
    req.update(_base_order_fields(symbol, order_type, price, sl, tp))
    res = mt5.order_send(req)
    if res:
        print("📤 ส่ง market:", res)
    return res


def send_order_pending(symbol: str, lot: float, side: str, ptype: str, entry_price: float, sl: float, tp: float):
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    if not tick or not info:
        print("❌ ไม่มี tick หรือ symbol_info")
        return None
    min_stop = (info.trade_stops_level or 0) * (info.point or 0)
    # pending type
    if side == "buy":
        if ptype == "LIMIT":
            otype = mt5.ORDER_TYPE_BUY_LIMIT
            # buy limit price MUST be <= current ask
            entry_price = min(entry_price, tick.ask - 0.5 * min_stop)
            sl = min(sl, entry_price - min_stop)
            tp = max(tp, entry_price + min_stop)
        else:  # STOP
            otype = mt5.ORDER_TYPE_BUY_STOP
            entry_price = max(entry_price, tick.ask + min_stop)
            sl = min(sl, entry_price - min_stop)
            tp = max(tp, entry_price + min_stop)
    else:
        if ptype == "LIMIT":
            otype = mt5.ORDER_TYPE_SELL_LIMIT
            entry_price = max(entry_price, tick.bid + 0.5 * min_stop)
            sl = max(sl, entry_price + min_stop)
            tp = min(tp, entry_price - min_stop)
        else:
            otype = mt5.ORDER_TYPE_SELL_STOP
            entry_price = min(entry_price, tick.bid - min_stop)
            sl = max(sl, entry_price + min_stop)
            tp = min(tp, entry_price - min_stop)

    req = {"action": mt5.TRADE_ACTION_PENDING, "volume": lot}
    req.update(_base_order_fields(symbol, otype, entry_price, sl, tp))
    res = mt5.order_send(req)
    if res:
        print("📤 ส่ง pending:", res)
    return res

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
        # Bullish BOS
        prev_high = win["high"].max()
        if tail["close"].iloc[i] > prev_high + atr_mult * ref_atr:
            j = i-1
            while j >= 1 and not (tail["close"].iloc[j] < tail["open"].iloc[j]):
                j -= 1
            if j >= 1:
                ob_low  = float(min(tail["open"].iloc[j], tail["close"].iloc[j], tail["low"].iloc[j]))
                ob_high = float(max(tail["open"].iloc[j], tail["close"].iloc[j]))
                bull_zone = (ob_low, ob_high)
        # Bearish BOS
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
    if bull_ob and touch_zone(entry, bull_ob, atr):
        zlo, zhi = bull_ob
        sl = min(entry - pad, zlo - pad)
        tp_candidates = [zhi + pad]
        if ctx_r and ctx_r > entry:
            tp_candidates.append(entry + ZONE_TP_FRAC * (ctx_r - entry))
        if range_bounds and range_bounds[1] and range_bounds[1] > entry:
            tp_candidates.append(entry + ZONE_TP_FRAC * (range_bounds[1] - entry))
        tp_candidates.append(entry + ZONE_TP_ATR * atr)
        tp = min([c for c in tp_candidates if c > entry])
        return sl, tp
    base = ctx_s if ctx_s is not None else (range_bounds[0] if range_bounds else None)
    if base is not None:
        sl = base - pad
        opp = None
        if ctx_r and ctx_r > entry:
            opp = ctx_r
        elif range_bounds and range_bounds[1] and range_bounds[1] > entry:
            opp = range_bounds[1]
        tp = entry + ZONE_TP_FRAC * (opp - entry) if opp else entry + ZONE_TP_ATR * atr
        return sl, tp
    return entry - ATR_SL_MULT * atr, entry + ATR_TP_MULT * atr


def zone_exits_sell(entry: float, atr: float,
                    ctx_s: Optional[float], ctx_r: Optional[float],
                    bear_ob: Optional[Tuple[float,float]],
                    range_bounds: Optional[Tuple[float,float]]) -> Tuple[float,float]:
    pad = ZONE_SL_PAD_ATR * atr
    if bear_ob and touch_zone(entry, bear_ob, atr):
        zlo, zhi = bear_ob
        sl = max(entry + pad, zhi + pad)
        tp_candidates = [zlo - pad]
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
        tp = entry - ZONE_TP_FRAC * (entry - opp) if opp else entry - ZONE_TP_ATR * atr
        return sl, tp
    return entry + ATR_SL_MULT * atr, entry - ATR_TP_MULT * atr

# ======================
# SCALE-OUT & BE SHIFT
# ======================

def manage_position_scaleout(symbol: str, atr_m5: float):
    if not SCALEOUT_ENABLED:
        return
    positions = mt5.positions_get(symbol=symbol) or []
    info = mt5.symbol_info(symbol)
    if not info:
        return
    min_vol = info.volume_min or 0.01
    for p in positions:
        if p.sl in (None, 0.0):
            continue
        R = abs((p.price_open or 0.0) - (p.sl or 0.0))
        if R <= 0 or atr_m5 is None or pd.isna(atr_m5):
            continue
        price_now = (mt5.symbol_info_tick(symbol).bid if p.type == mt5.POSITION_TYPE_BUY else mt5.symbol_info_tick(symbol).ask)
        reached_tp1 = (p.type == mt5.POSITION_TYPE_BUY and price_now >= p.price_open + TP1_R * R) or \
                      (p.type == mt5.POSITION_TYPE_SELL and price_now <= p.price_open - TP1_R * R)
        if reached_tp1 and p.volume > min_vol + 1e-9:
            # ปิดครึ่งหนึ่ง
            close_volume = max(min_vol, round(p.volume / 2.0 / (info.volume_step or 0.01)) * (info.volume_step or 0.01))
            close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
            close_price = mt5.symbol_info_tick(symbol).bid if close_type == mt5.ORDER_TYPE_SELL else mt5.symbol_info_tick(symbol).ask
            mt5.order_send({
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": close_volume,
                "type": close_type,
                "price": close_price,
                "deviation": 20,
                "magic": 123456,
                "comment": "TP1 partial",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": get_supported_filling_mode(symbol) or mt5.ORDER_FILLING_RETURN
            })
            # เลื่อน SL → BE + pad
            be = p.price_open + (BE_PAD_ATR * atr_m5 if p.type == mt5.POSITION_TYPE_BUY else -BE_PAD_ATR * atr_m5)
            mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": p.ticket, "sl": be, "tp": p.tp})

# ======================
# MAIN LOOP
# ======================

def main_loop():
    global TRADES_TODAY
    last_trade_time = {"buy": datetime.min, "sell": datetime.min}
    try:
        while True:
            # -------- Load Data (H1, M15, M5) --------
            df_h1  = get_data(SYMBOL, timeframe=MAJOR_TF,   n=HISTORY_MAJOR)
            df_m15 = get_data(SYMBOL, timeframe=CONTEXT_TF, n=HISTORY_CONTEXT)
            df_m5  = get_data(SYMBOL, timeframe=SIGNAL_TF,  n=HISTORY_SIGNAL)
            if df_h1.empty or df_m15.empty or df_m5.empty:
                time.sleep(30); continue

            # Indicators
            for d in (df_h1, df_m15, df_m5):
                d["rsi"] = calculate_rsi(d)
            atr_m5_series  = calculate_atr(df_m5)
            atr_m15_series = calculate_atr(df_m15)
            atr_m5         = atr_m5_series.iloc[-1]

            # Filters (volatility)
            # if not volatility_ok(atr_m5_series):
            #     print("⛔ Volatility regime ไม่เหมาะสม"); time.sleep(15); continue

            # Trend & Levels by TF
            major_s, major_r, major_trend, major_fib, _ = find_support_resistance_advanced(df_h1)
            ctx_s,   ctx_r,   ctx_trend,   ctx_fib,   rsi_m15 = find_support_resistance_advanced(df_m15)
            sig_s,   sig_r,   sig_trend,   sig_fib,   rsi_m5  = find_support_resistance_advanced(df_m5)
            if None in (major_s, major_r, major_trend, ctx_s, ctx_r, ctx_trend, sig_s, sig_r, sig_trend, rsi_m5) or pd.isna(atr_m5):
                time.sleep(10); continue

            # OB zones on M15
            bull_ob, bear_ob = (None, None)
            if ENABLE_ORDER_BLOCK:
                bull_ob, bear_ob = find_recent_order_blocks(df_m15, atr_m15_series, OB_LOOKBACK_BARS, OB_BOS_LOOKBACK, OB_ATR_MULT)

            # Range bounds
            range_low, range_high = (None, None)
            if ENABLE_RANGE_MODE:
                range_low, range_high = compute_range_bounds(df_m15, RANGE_LOOKBACK_BARS)

            trending_mode = (major_trend == ctx_trend) and (ctx_trend in ("uptrend", "downtrend"))
            range_mode    = ENABLE_RANGE_MODE and (ctx_trend == "sideways") and (range_low is not None) and (range_high is not None)

            # Risk / Discipline gates
            account = mt5.account_info()
            if not account:
                time.sleep(5); continue
            update_daily_counters(account.equity)
            # if not daily_limits_ok(account.equity):
            #     time.sleep(60); continue
            
            risk_pct = dynamic_risk_pct(account.equity)

            # if not is_good_time_to_trade():
            #     print("⏳ Outside allowed session/news or avoid-hours"); time.sleep(20); continue

            # if not spread_ok(SYMBOL, atr_m5):
            #     print("⛔ สเปรดกว้างเกิน ATR-ratio"); time.sleep(10); continue

            # Manage trailing & scale-outs
            modify_order_trailing(SYMBOL)
            manage_position_scaleout(SYMBOL, atr_m5)

            tick = mt5.symbol_info_tick(SYMBOL)
            if not tick:
                time.sleep(5); continue
            bid_price, ask_price = tick.bid, tick.ask

            positions = mt5.positions_get(symbol=SYMBOL) or []
            has_buy  = sum(1 for p in positions if p.type == mt5.POSITION_TYPE_BUY)
            has_sell = sum(1 for p in positions if p.type == mt5.POSITION_TYPE_SELL)

            vol_ok  = is_volume_spike(df_m5)
            div_b   = detect_rsi_divergence_bull(df_m5)
            div_s   = detect_rsi_divergence_bear(df_m5)
            bos_up, bos_dn = m5_structure_trigger(df_m5)

            print(
                f"📊 Bid:{bid_price:.2f} Ask:{ask_price:.2f} Trend(H1/M15/M5): {major_trend}/{ctx_trend}/{sig_trend} "
                f"S(ctx/sig): {ctx_s:.2f}/{sig_s:.2f} R(ctx/sig): {ctx_r:.2f}/{sig_r:.2f} RSI(M5):{rsi_m5:.2f} ATR(M5):{atr_m5:.2f} Vol:{vol_ok} Div(B/S):{div_b}/{div_s} BOS(up/dn):{bos_up}/{bos_dn}"
            )
            if bull_ob or bear_ob:
                print(f"🧱 OB M15 → Bull:{bull_ob} Bear:{bear_ob}")
            if range_low is not None and range_high is not None:
                print(f"📦 Range M15 → Low:{range_low:.2f} High:{range_high:.2f}")

            # Stop if no mode selected
            if not trending_mode and not range_mode:
                print(f"⚡ ยังไม่เข้าโหมดเทรด | H1={major_trend} M15={ctx_trend}")
                time.sleep(8); continue

            stop_dist = ATR_SL_MULT * atr_m5  # สำหรับ sizing และ fallback
            buy_lot  = calc_lot_from_risk(SYMBOL, risk_pct, account.balance, stop_dist)
            sell_lot = calc_lot_from_risk(SYMBOL, risk_pct, account.balance, stop_dist)

            now = datetime.now()
            can_buy_cd  = (now - last_trade_time["buy"]).total_seconds()  >= COOLDOWN_SEC
            can_sell_cd = (now - last_trade_time["sell"]).total_seconds() >= COOLDOWN_SEC

            def zone_key(name: str, lo: Optional[float], hi: Optional[float] = None) -> str:
                if hi is None:
                    return f"{name}:{None if lo is None else round(lo,1)}"
                return f"{name}:{round(lo,1)}-{round(hi,1)}"

            def zone_available(key: str) -> bool:
                ts = LAST_ZONE_TRADE_AT.get(key)
                if not ts:
                    return True
                return (now - ts) >= timedelta(minutes=ZONE_REENTRY_COOLDOWN_MIN)

            trade_done = False
            trend_str = f"H1/M15/M5={major_trend}/{ctx_trend}/{sig_trend}"

            # -------------- TRENDING MODE --------------
            if trending_mode:
                # BUY
                if ctx_trend == "uptrend" and sig_trend in ("uptrend",) and rsi_m5 < 70 and (bos_up):
                    key = zone_key("BUY_ctxS", ctx_s)
                    if (has_buy < MAX_OPEN_SIDE) and can_buy_cd and zone_available(key):
                        # exits
                        if EXIT_MODE == "ZONE":
                            sl, tp = zone_exits_buy(ask_price, atr_m5, ctx_s, ctx_r, bull_ob, (range_low, range_high) if range_low and range_high else None)
                        else:
                            sl, tp = ask_price - ATR_SL_MULT * atr_m5, ask_price + ATR_TP_MULT * atr_m5
                        # ENTRY TYPE
                        if ENTRY_MODE == "MARKET":
                            res = send_order_market(SYMBOL, buy_lot, "buy", sl, tp)
                        else:
                            # derive limit/stop price
                            ref = ctx_s if ctx_s is not None else (bull_ob[0] if bull_ob else ask_price)
                            limit_price = min(ask_price - ENTRY_OFFSET_ATR * atr_m5, ref)
                            stop_price  = ask_price + ENTRY_OFFSET_ATR * atr_m5
                            if ENTRY_MODE == "LIMIT":
                                res = send_order_pending(SYMBOL, buy_lot, "buy", "LIMIT", limit_price, sl, tp)
                            else:
                                res = send_order_pending(SYMBOL, buy_lot, "buy", "STOP",  stop_price,  sl, tp)
                        log_trade("BUY", ask_price, trend_str, rsi_m5, ctx_s, ctx_r, {**ctx_fib, **sig_fib}, res)
                        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                            last_trade_time["buy"] = now; LAST_ZONE_TRADE_AT[key] = now; TRADES_TODAY += 1; trade_done = True

                # SELL
                if not trade_done and ctx_trend == "downtrend" and sig_trend in ("downtrend",) and rsi_m5 > 30 and (bos_dn):
                    key = zone_key("SELL_ctxR", ctx_r)
                    if (has_sell < MAX_OPEN_SIDE) and can_sell_cd and zone_available(key):
                        if EXIT_MODE == "ZONE":
                            sl, tp = zone_exits_sell(bid_price, atr_m5, ctx_s, ctx_r, bear_ob, (range_low, range_high) if range_low and range_high else None)
                        else:
                            sl, tp = bid_price + ATR_SL_MULT * atr_m5, bid_price - ATR_TP_MULT * atr_m5
                        if ENTRY_MODE == "MARKET":
                            res = send_order_market(SYMBOL, sell_lot, "sell", sl, tp)
                        else:
                            ref = ctx_r if ctx_r is not None else (bear_ob[1] if bear_ob else bid_price)
                            limit_price = max(bid_price + ENTRY_OFFSET_ATR * atr_m5, ref)
                            stop_price  = bid_price - ENTRY_OFFSET_ATR * atr_m5
                            if ENTRY_MODE == "LIMIT":
                                res = send_order_pending(SYMBOL, sell_lot, "sell", "LIMIT", limit_price, sl, tp)
                            else:
                                res = send_order_pending(SYMBOL, sell_lot, "sell", "STOP",  stop_price,  sl, tp)
                        log_trade("SELL", bid_price, trend_str, rsi_m5, ctx_s, ctx_r, {**ctx_fib, **sig_fib}, res)
                        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                            last_trade_time["sell"] = now; LAST_ZONE_TRADE_AT[key] = now; TRADES_TODAY += 1; trade_done = True

            # -------------- RANGE MODE --------------
            if not trade_done and range_mode:
                # BUY near range low (need confirmation: volume or bull div)
                if has_buy < MAX_OPEN_SIDE and can_buy_cd:
                    cond_price = near_by_atr(ask_price, range_low, atr_m5) or (bull_ob and touch_zone(ask_price, bull_ob, atr_m5))
                    cond_conf  = vol_ok or div_b or bos_up
                    if cond_price and cond_conf and rsi_m5 <= RANGE_RSI_BUY_MAX:
                        key = zone_key("BUY_range", range_low)
                        if zone_available(key):
                            if EXIT_MODE == "ZONE":
                                sl, tp = zone_exits_buy(ask_price, atr_m5, ctx_s, ctx_r, bull_ob, (range_low, range_high))
                            else:
                                sl, tp = ask_price - ATR_SL_MULT * atr_m5, ask_price + ATR_TP_MULT * atr_m5
                            if ENTRY_MODE == "MARKET":
                                res = send_order_market(SYMBOL, buy_lot, "buy", sl, tp)
                            else:
                                ref = range_low if range_low is not None else ask_price
                                limit_price = min(ask_price - ENTRY_OFFSET_ATR * atr_m5, ref)
                                stop_price  = ask_price + ENTRY_OFFSET_ATR * atr_m5
                                if ENTRY_MODE == "LIMIT":
                                    res = send_order_pending(SYMBOL, buy_lot, "buy", "LIMIT", limit_price, sl, tp)
                                else:
                                    res = send_order_pending(SYMBOL, buy_lot, "buy", "STOP",  stop_price,  sl, tp)
                            log_trade("BUY_RANGE", ask_price, trend_str, rsi_m5, range_low, range_high, {**ctx_fib, **sig_fib}, res)
                            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                                last_trade_time["buy"] = now; LAST_ZONE_TRADE_AT[key] = now; TRADES_TODAY += 1; trade_done = True

                # SELL near range high
                if not trade_done and has_sell < MAX_OPEN_SIDE and can_sell_cd:
                    cond_price = near_by_atr(bid_price, range_high, atr_m5) or (bear_ob and touch_zone(bid_price, bear_ob, atr_m5))
                    cond_conf  = vol_ok or div_s or bos_dn
                    if cond_price and cond_conf and rsi_m5 >= RANGE_RSI_SELL_MIN:
                        key = zone_key("SELL_range", range_high)
                        if zone_available(key):
                            if EXIT_MODE == "ZONE":
                                sl, tp = zone_exits_sell(bid_price, atr_m5, ctx_s, ctx_r, bear_ob, (range_low, range_high))
                            else:
                                sl, tp = bid_price + ATR_SL_MULT * atr_m5, bid_price - ATR_TP_MULT * atr_m5
                            if ENTRY_MODE == "MARKET":
                                res = send_order_market(SYMBOL, sell_lot, "sell", sl, tp)
                            else:
                                ref = range_high if range_high is not None else bid_price
                                limit_price = max(bid_price + ENTRY_OFFSET_ATR * atr_m5, ref)
                                stop_price  = bid_price - ENTRY_OFFSET_ATR * atr_m5
                                if ENTRY_MODE == "LIMIT":
                                    res = send_order_pending(SYMBOL, sell_lot, "sell", "LIMIT", limit_price, sl, tp)
                                else:
                                    res = send_order_pending(SYMBOL, sell_lot, "sell", "STOP",  stop_price,  sl, tp)
                            log_trade("SELL_RANGE", bid_price, trend_str, rsi_m5, range_low, range_high, {**ctx_fib, **sig_fib}, res)
                            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                                last_trade_time["sell"] = now; LAST_ZONE_TRADE_AT[key] = now; TRADES_TODAY += 1; trade_done = True

            if not trade_done:
                print("📉 ยังไม่ครบเงื่อนไข/มีโพซิชันอยู่แล้ว/คูลดาวน์อยู่")

            print("⏳ รอรอบถัดไป...\n")
            time.sleep(60)

    except KeyboardInterrupt:
        print("\n🛑 หยุดด้วย KeyboardInterrupt")
    finally:
        mt5.shutdown()
        print("🔌 ปิดการเชื่อมต่อ MT5")


# ======================
# (Optional) Simple Analytics
# ======================

def summarize_log(logfile: str = "trade_log.csv"):
    if not os.path.exists(logfile):
        print("ไม่มีไฟล์ log ให้สรุปผล")
        return
    df = pd.read_csv(logfile)
    if df.empty:
        print("log ว่างเปล่า")
        return
    print("\n==== Summary ====")
    print("Total trades:", len(df))
    print(df["action"].value_counts())
    # หมายเหตุ: ต้องเชื่อมผลกำไร/ขาดทุนจริงจาก account history เพื่อคำนวณ metrics ลึกขึ้น


if __name__ == "__main__":
    main_loop()
