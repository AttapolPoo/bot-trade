import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import threading
import time
import os
import csv
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional, Tuple, List, Callable
import traceback  # for detailed error logs

# ====== Tkinter GUI ======
import tkinter as tk
from tkinter import ttk, filedialog
from tkinter.scrolledtext import ScrolledText

# ====== Optional matplotlib for equity chart ======
HAVE_MPL = True
try:
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
except Exception:
    HAVE_MPL = False


"""
Windows App: Scalp Bot (Order Block + Confirmation) — Extended + Breakeven + Trade-only Logs + P&L Report
- Log เฉพาะตอน "เข้าออเดอร์" + บันทึก CSV
- แท็บ "P&L Report" สรุปกำไร/ขาดทุนจาก deals (DEAL_ENTRY_OUT) + Export CSV
- Heartbeat/Spinner ใน Bots Manager แสดงว่าบอทยังทำงานจริง และเตือน Stalled
"""

# ======================
# MT5 retcodes / constants
# ======================
RETCODE_DONE            = getattr(mt5, "TRADE_RETCODE_DONE", 10009)
RETCODE_PLACED          = getattr(mt5, "TRADE_RETCODE_PLACED", 10008)
RETCODE_REQUOTE         = getattr(mt5, "TRADE_RETCODE_REQUOTE", 10004)
RETCODE_PRICE_CHANGED   = getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", 10032)

DEAL_ENTRY_IN           = getattr(mt5, "DEAL_ENTRY_IN", 0)
DEAL_ENTRY_OUT          = getattr(mt5, "DEAL_ENTRY_OUT", 1)

# magic สำหรับบอทนี้
BOT_MAGIC = 246810

# ====== Default Set (Bot Manager) ======
AUTO_CREATE_DEFAULTS_ON_LAUNCH = True   # สร้างชุด Default ตอนเปิดแอป
AUTO_START_DEFAULTS_ON_LAUNCH  = True   # ให้เริ่มรันทันทีเหมือนในรูป

DEFAULT_SYMBOL   = "GOLD#"
DEFAULT_TFS      = ("M1", "M5", "M15")
DEFAULT_TPMODES  = ("FIXED", "RANGE_RANDOM", "ADAPTIVE_TO_ZONE")
DEFAULT_LOT      = 0.02
DEFAULT_TP       = 300
DEFAULT_SL       = 150
DEFAULT_SPREAD   = 120
DEFAULT_COOLDOWN = 30
DEFAULT_MAXPOS   = 5
DEFAULT_MAXHOLD  = 900
DEFAULT_SIDE     = "BOTH"


# ======================
# Thread-safe MT5 connection manager
# ======================
_mt5_lock = threading.Lock()
_mt5_trade_lock = threading.Lock()
_mt5_init_count = 0
_last_be_mod = {}  # ticket -> last modified datetime (breakeven throttle)

# cache โหมดการ fill ต่อสัญลักษณ์ (จำโหมดที่ "ใช้ได้จริง")
_FILLING_CACHE: dict[str, int] = {}

def mt5_connect() -> bool:
    global _mt5_init_count
    with _mt5_lock:
        if _mt5_init_count == 0:
            if not mt5.initialize():
                return False
        _mt5_init_count += 1
        return True

def mt5_disconnect():
    global _mt5_init_count
    with _mt5_lock:
        _mt5_init_count = max(0, _mt5_init_count - 1)
        if _mt5_init_count == 0:
            try:
                mt5.shutdown()
            except:
                pass

# ======================
# Strategy Config
# ======================
@dataclass
class BotConfig:
    symbol: str = "GOLD#"
    tf_name: str = "M1"  # "M1" | "M5" | "M15"
    history_bars: int = 1200

    lot_fixed: float = 0.02

    # TP/SL (points)
    tp_mode: str = "FIXED"      # "FIXED" | "RANGE_RANDOM" | "ADAPTIVE_TO_ZONE"
    tp_points_fixed: int = 300
    sl_points_fixed: int = 150
    tp_points_min: int = 100
    tp_points_max: int = 300

    # Filters / limits
    spread_max_points: int = 120
    cooldown_sec: int = 30
    max_open_pos: int = 5
    max_hold_sec: int = 900  # 0=off

    # OB detection
    ob_lookback_bars: int = 200
    bos_lookback: int = 8
    ob_pad_points: int = 20

    # Confirmation
    confirm_use_rejection: bool = True
    confirm_use_bos: bool = True
    confirm_mode: str = "ANY"          # "ANY" | "BOTH"
    bos_confirm_lookback: int = 5
    rej_min_wick_frac: float = 0.55
    rej_max_body_frac: float = 0.35
    rej_min_range_points: int = 20

    # SR (เฉพาะใช้กับโหมด TP ADAPTIVE)
    sr_lookback: int = 80

    # Side mode
    side_mode: str = "BOTH"            # "BOTH" | "BUY_ONLY" | "SELL_ONLY"

    # Breakeven settings
    be_enable: bool = True
    be_trigger_points: int = 100
    be_offset_points: int = 5
    be_once: bool = False

    # ===== NEW: SMC/Indicators =====
    use_fvg: bool = False
    fvg_lookback: int = 60
    fvg_min_gap_points: int = 10

    use_sweep: bool = False
    sweep_lookback: int = 30
    sweep_tolerance_pts: int = 10

    use_macd: bool = False
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    # ต้องผ่านกี่เงื่อนไข (rej/BOS/sweep/FVG) ขั้นต่ำ
    confirm_min_score: int = 1

TF_MAP = {
    "M1":  mt5.TIMEFRAME_M1,
    "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
}

# ======================
# Helpers (market)
# ======================
def get_data(symbol: str, timeframe, n: int) -> pd.DataFrame:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", errors="coerce")
    return df

def round_price(symbol: str, price: float) -> float:
    info = mt5.symbol_info(symbol)
    if not info: return float(price)
    digits = info.digits or 2
    return float(round(price, digits))

def normalize_volume(symbol: str, vol: float) -> float:
    info = mt5.symbol_info(symbol)
    if not info: return float(vol)
    step = info.volume_step or 0.01
    vmin = info.volume_min or step
    vmax = info.volume_max or vol
    return float(max(vmin, min(vmax, round(vol/step)*step)))

def spread_ok(symbol: str, max_points: int) -> bool:
    info = mt5.symbol_info(symbol)
    if not info:
        return True
    return (info.spread or 0) <= max_points

def touch_zone_points(price: float, zone: Optional[Tuple[float,float]], pad_points: float, point: float) -> bool:
    if zone is None or point <= 0:
        return False
    lo, hi = zone
    if lo is None or hi is None:
        return False
    lo -= pad_points * point
    hi += pad_points * point
    return lo <= price <= hi

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def find_order_blocks(df: pd.DataFrame,
                      lookback: int,
                      bos_back: int) -> Tuple[Optional[Tuple[float,float]], Optional[Tuple[float,float]]]:
    if len(df) < max(lookback, bos_back+3):
        return None, None
    t = df.tail(lookback).reset_index(drop=True)
    bull_ob = None
    bear_ob = None
    for i in range(bos_back+3, len(t)):
        win = t.iloc[i-bos_back:i]
        prev_high = win["high"].max()
        if t["close"].iloc[i] > prev_high:
            j = i-1
            while j >= 1 and not (t["close"].iloc[j] < t["open"].iloc[j]):
                j -= 1
            if j >= 1:
                lo = float(min(t["open"].iloc[j], t["close"].iloc[j], t["low"].iloc[j]))
                hi = float(max(t["open"].iloc[j], t["close"].iloc[j]))
                bull_ob = (lo, hi)
        prev_low = win["low"].min()
        if t["close"].iloc[i] < prev_low:
            j = i-1
            while j >= 1 and not (t["close"].iloc[j] > t["open"].iloc[j]):
                j -= 1
            if j >= 1:
                lo = float(min(t["open"].iloc[j], t["close"].iloc[j]))
                hi = float(max(t["open"].iloc[j], t["close"].iloc[j], t["high"].iloc[j]))
                bear_ob = (lo, hi)
    return bull_ob, bear_ob

def last_candle_features(df: pd.DataFrame, point: float):
    o = float(df["open"].iloc[-1]); h = float(df["high"].iloc[-1])
    l = float(df["low"].iloc[-1]);  c = float(df["close"].iloc[-1])
    rng = h - l
    body = abs(c - o)
    upper = h - max(c, o)
    lower = min(c, o) - l
    rng_pts = rng / point if point > 0 else 0.0
    body_pts = body / point if point > 0 else 0.0
    up_pts   = upper / point if point > 0 else 0.0
    lo_pts   = lower / point if point > 0 else 0.0
    return dict(open=o, high=h, low=l, close=c,
                range=rng, body=body, upper=upper, lower=lower,
                range_pts=rng_pts, body_pts=body_pts, upper_pts=up_pts, lower_pts=lo_pts)

def is_bullish_rejection(df: pd.DataFrame, point: float, cfg: BotConfig) -> bool:
    f = last_candle_features(df, point)
    if f["range_pts"] < cfg.rej_min_range_points:
        return False
    bullish_close = f["close"] > f["open"] or f["close"] >= (f["low"] + 0.6*(f["range"]))
    long_lower = (f["lower"] >= cfg.rej_min_wick_frac * f["range"]) and (f["body"] <= cfg.rej_max_body_frac * f["range"])
    return bool(bullish_close and long_lower)

def is_bearish_rejection(df: pd.DataFrame, point: float, cfg: BotConfig) -> bool:
    f = last_candle_features(df, point)
    if f["range_pts"] < cfg.rej_min_range_points:
        return False
    bearish_close = f["close"] < f["open"] or f["close"] <= (f["high"] - 0.6*(f["range"]))
    long_upper = (f["upper"] >= cfg.rej_min_wick_frac * f["range"]) and (f["body"] <= cfg.rej_max_body_frac * f["range"])
    return bool(bearish_close and long_upper)

def mini_bos_up(df: pd.DataFrame, lookback: int) -> bool:
    if len(df) < lookback + 2:
        return False
    prev_high = df["high"].iloc[-(lookback+1):-1].max()
    return float(df["close"].iloc[-1]) > float(prev_high)

def mini_bos_down(df: pd.DataFrame, lookback: int) -> bool:
    if len(df) < lookback + 2:
        return False
    prev_low = df["low"].iloc[-(lookback+1):-1].min()
    return float(df["close"].iloc[-1]) < float(prev_low)

def choose_tp_points(info,
                     price_now: float,
                     side: str,
                     s: Optional[float],
                     r: Optional[float],
                     bull_ob: Optional[Tuple[float,float]],
                     bear_ob: Optional[Tuple[float,float]],
                     cfg: BotConfig = None) -> int:
    if cfg is None:
        stops_level_pts = info.trade_stops_level or 0
        return max(300, stops_level_pts+1)
    stops_level_pts = info.trade_stops_level or 0
    if cfg.tp_mode == "FIXED":
        tp_pts = cfg.tp_points_fixed
    elif cfg.tp_mode == "RANGE_RANDOM":
        tp_pts = int(np.random.randint(cfg.tp_points_min, cfg.tp_points_max+1))
    else:
        point = info.point or 0.01
        if side == "buy":
            targets = []
            if r: targets.append(abs(r - price_now) / point)
            if bear_ob: targets.append(abs(bear_ob[0] - price_now) / point)
            tp_pts = int(clamp(min(targets) if targets else cfg.tp_points_fixed,
                               cfg.tp_points_min, cfg.tp_points_max))
        else:
            targets = []
            if s: targets.append(abs(price_now - s) / point)
            if bull_ob: targets.append(abs(price_now - bull_ob[1]) / point)
            tp_pts = int(clamp(min(targets) if targets else cfg.tp_points_fixed,
                               cfg.tp_points_min, cfg.tp_points_max))
    return max(tp_pts, stops_level_pts + 1)

# ---------- MACD ----------
def macd(series: pd.Series, fast=12, slow=26, signal=9):
    ema_f = series.ewm(span=fast, adjust=False).mean()
    ema_s = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_f - ema_s
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist

# ---------- FVG (Fair Value Gap) ----------
def detect_fvg(df: pd.DataFrame, lookback: int, min_gap_pts: int, point: float):
    """
    หา FVG ล่าสุดภายในช่วง lookback:
      - Bullish FVG: low[i] > high[i-2]
      - Bearish FVG: high[i] < low[i-2]
    คืนค่า zone เป็น (lo, hi) ของช่องว่างแต่ละฝั่ง (เอาตัวล่าสุด)
    """
    if len(df) < 3: return None, None
    start = max(2, len(df) - lookback)
    bull, bear = None, None
    for i in range(start, len(df)):
        # Bullish FVG
        if df["low"].iloc[i] > df["high"].iloc[i-2]:
            gap_pts = (df["low"].iloc[i] - df["high"].iloc[i-2]) / (point or 0.01)
            if gap_pts >= min_gap_pts:
                bull = (float(df["high"].iloc[i-2]), float(df["low"].iloc[i]))  # [lo, hi]
        # Bearish FVG
        if df["high"].iloc[i] < df["low"].iloc[i-2]:
            gap_pts = (df["low"].iloc[i-2] - df["high"].iloc[i]) / (point or 0.01)
            if gap_pts >= min_gap_pts:
                bear = (float(df["high"].iloc[i]), float(df["low"].iloc[i-2]))  # [lo, hi]
    return bull, bear

# ---------- Liquidity Sweep / SFP ----------
def detect_liquidity_sweep(df: pd.DataFrame, point: float,
                           lookback: int, tol_pts: int, use_closed_bar=True):
    """
    มองหาการกินสภาพคล่องแบบง่าย:
      - Bearish sweep: แท่งล่าสุดไส้บนทะลุ swing-high ก่อนหน้า >= tol แล้วปิดกลับ "ใต้" high เดิม → bias ขาย
      - Bullish sweep: ไส้ล่างหลุด swing-low ก่อนหน้า >= tol แล้วปิดกลับ "เหนือ" low เดิม → bias ซื้อ
    """
    if len(df) < lookback + 3: 
        return False, False  # bull_sweep, bear_sweep

    idx = -2 if use_closed_bar else -1
    o = float(df["open"].iloc[idx]); h = float(df["high"].iloc[idx])
    l = float(df["low"].iloc[idx]);  c = float(df["close"].iloc[idx])

    win = df.iloc[-(lookback+2):idx]    # ย้อนหลังจนถึงก่อนแท่งที่ใช้ตัดสิน
    prev_high = float(win["high"].max())
    prev_low  = float(win["low"].min())
    tol = tol_pts * (point or 0.01)

    bear_sweep = (h > prev_high + tol) and (c < prev_high)     # กินเหนือแล้วปิดกลับลง
    bull_sweep = (l < prev_low  - tol) and (c > prev_low)      # กินใต้แล้วปิดกลับขึ้น
    return bool(bull_sweep), bool(bear_sweep)

# ---------- ยูทิลเพื่อเช็คแตะโซน FVG ----------
def touch_zone(price: float, zone: Optional[Tuple[float,float]], pad_pts: int, point: float) -> bool:
    if not zone or point <= 0: return False
    lo, hi = zone
    lo -= pad_pts * point
    hi += pad_pts * point
    return lo <= price <= hi


# ======================
# Filling-mode helpers & robust sender (with cache)
# ======================
def _filling_name(v: Optional[int]) -> str:
    try:
        if v == mt5.ORDER_FILLING_FOK: return "FOK"
        if v == mt5.ORDER_FILLING_IOC: return "IOC"
        if v == mt5.ORDER_FILLING_RETURN: return "RETURN"
    except Exception:
        pass
    return "UNKNOWN" if v is None else f"UNKNOWN({v})"

def _candidates_fillings(symbol: str) -> List[int]:
    """เรียงลำดับ: cache → IOC → FOK → RETURN → (ค่าจากโบรก) → unique"""
    cand: List[int] = []
    cached = _FILLING_CACHE.get(symbol)
    if isinstance(cached, int):
        cand.append(cached)
    pref = [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]
    cand.extend([x for x in pref if x not in cand])

    info = mt5.symbol_info(symbol)
    if info:
        fm = getattr(info, "filling_mode", None)
        if isinstance(fm, int) and fm not in cand:
            cand.append(fm)
        mask = getattr(info, "fillings", None)
        if isinstance(mask, int):
            if (mask & 1) and (mt5.ORDER_FILLING_FOK not in cand): cand.append(mt5.ORDER_FILLING_FOK)
            if (mask & 2) and (mt5.ORDER_FILLING_IOC not in cand): cand.append(mt5.ORDER_FILLING_IOC)
            if (mask & 4) and (mt5.ORDER_FILLING_RETURN not in cand): cand.append(mt5.ORDER_FILLING_RETURN)
    seen=set(); out=[]
    for m in cand:
        if m not in seen:
            seen.add(m); out.append(m)
    return out

def _order_check_and_send_force(symbol: str, req_base: dict, logger=lambda *_: None):
    """
    กำหนด type_filling แบบ explicit เท่านั้น + ยอมรับ order_check retcode 0/DONE/PLACED
    """
    fillings = _candidates_fillings(symbol)
    last_chk = None
    for ff in fillings:
        req = dict(req_base)
        req["type_filling"] = ff

        with _mt5_trade_lock:
            chk = mt5.order_check(req)
        last_chk = chk

        rc = getattr(chk, "retcode", None)
        if chk and rc in (0, RETCODE_DONE, RETCODE_PLACED):
            with _mt5_trade_lock:
                res = mt5.order_send(req)

            if res and getattr(res, "retcode", None) in (RETCODE_REQUOTE, RETCODE_PRICE_CHANGED):
                tick = mt5.symbol_info_tick(symbol)
                if tick:
                    if req["type"] == mt5.ORDER_TYPE_BUY:
                        req["price"] = round_price(symbol, tick.ask)
                    else:
                        req["price"] = round_price(symbol, tick.bid)
                    with _mt5_trade_lock:
                        res = mt5.order_send(req)

            # cache โหมดที่ใช้ได้จริง
            try:
                if res and getattr(res, "retcode", None) in (RETCODE_DONE, RETCODE_PLACED):
                    _FILLING_CACHE[symbol] = ff
            except Exception:
                pass
            return res, req

    info = mt5.symbol_info(symbol)
    if info:
        logger(f"filling_mode={getattr(info,'filling_mode',None)} "
               f"fillings={getattr(info,'fillings',None)} trade_mode={getattr(info,'trade_mode',None)}")
    return last_chk, req_base

# ======================
# Market order & close with enforced filling
# ======================
def send_market_order(symbol: str, side: str, lot: float, sl_pts: int, tp_pts: int, logger=lambda *_: None):
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not tick or not info:
        logger("no tick/info")
        return None, None
    point = info.point or 0.01
    price = tick.ask if side=="buy" else tick.bid
    price = round_price(symbol, price)

    mind = (max(getattr(info, "trade_stops_level", 0), getattr(info, "freeze_level", 0)) * point) + point

    if side=="buy":
        tp = price + max(tp_pts*point, mind)
        sl = price - max(sl_pts*point, mind)
        otype = mt5.ORDER_TYPE_BUY
    else:
        tp = price - max(tp_pts*point, mind)
        sl = price + max(sl_pts*point, mind)
        otype = mt5.ORDER_TYPE_SELL

    tp = round_price(symbol, tp); sl = round_price(symbol, sl)
    lot = normalize_volume(symbol, lot)

    deviation = max(60, int((info.spread or 0)*3))

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot),
        "type": otype,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": deviation,
        "magic": BOT_MAGIC,
        "comment": f"Scalp SL{sl_pts} TP{tp_pts}",
        "type_time": mt5.ORDER_TIME_GTC,
    }
    return _order_check_and_send_force(symbol, req, logger)

def close_stale_positions(symbol: str, max_age_sec: int, logger=lambda *_: None):
    if max_age_sec <= 0: return
    now = datetime.now()
    poses = mt5.positions_get(symbol=symbol) or []
    for p in poses:
        opened = datetime.fromtimestamp(int(p.time))
        age = (now - opened).total_seconds()
        if age >= max_age_sec:
            info = mt5.symbol_info(symbol); tick = mt5.symbol_info_tick(symbol)
            if not info or not tick: continue
            close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
            close_price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask
            close_price = round_price(symbol, close_price)
            deviation = max(60, int((info.spread or 0) * 3))
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": float(p.volume),
                "type": close_type,
                "price": close_price,
                "deviation": deviation,
                "magic": BOT_MAGIC,
                "comment": "Stale close",
                "type_time": mt5.ORDER_TIME_GTC,
            }
            _order_check_and_send_force(symbol, req, logger)

# ======================
# Modify SL/TP (for breakeven)
# ======================
def modify_position_sltp(position, new_sl: Optional[float], new_tp: Optional[float], logger=lambda *_: None):
    req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": position.ticket,
        "symbol": position.symbol,
        "sl": float(new_sl if new_sl is not None else position.sl),
        "tp": float(new_tp if new_tp is not None else position.tp),
    }
    with _mt5_trade_lock:
        res = mt5.order_send(req)
    return res

def manage_breakeven_for_symbol(cfg: BotConfig, logger=lambda *_: None):
    if not cfg.be_enable:
        return
    info = mt5.symbol_info(cfg.symbol)
    tick = mt5.symbol_info_tick(cfg.symbol)
    if not info or not tick:
        return
    point = info.point or 0.01
    min_dist = (getattr(info, "trade_stops_level", 0) * point)
    tol = point * 0.2
    now = datetime.now()

    poses = mt5.positions_get(symbol=cfg.symbol) or []
    for p in poses:
        last = _last_be_mod.get(p.ticket, datetime.min)
        if (now - last).total_seconds() < 5:
            continue

        if p.type == mt5.POSITION_TYPE_BUY:
            float_pts = (tick.bid - p.price_open) / point
            if float_pts < cfg.be_trigger_points:
                continue
            target_sl = round_price(cfg.symbol, p.price_open + cfg.be_offset_points * point)
            max_allowed_sl = round_price(cfg.symbol, tick.bid - (min_dist + point))
            new_sl = min(target_sl, max_allowed_sl)
            if new_sl > 0 and (p.sl == 0.0 or new_sl > p.sl + tol):
                res = modify_position_sltp(p, new_sl, None, logger)
                if res and getattr(res, "retcode", None) in (RETCODE_DONE, RETCODE_PLACED):
                    _last_be_mod[p.ticket] = now
        else:  # SELL
            float_pts = (p.price_open - tick.ask) / point
            if float_pts < cfg.be_trigger_points:
                continue
            target_sl = round_price(cfg.symbol, p.price_open - cfg.be_offset_points * point)
            min_allowed_sl = round_price(cfg.symbol, tick.ask + (min_dist + point))
            new_sl = max(target_sl, min_allowed_sl)
            if new_sl > 0 and (p.sl == 0.0 or new_sl < p.sl - tol):
                res = modify_position_sltp(p, new_sl, None, logger)
                if res and getattr(res, "retcode", None) in (RETCODE_DONE, RETCODE_PLACED):
                    _last_be_mod[p.ticket] = now

# ======================
# Bot Runner (thread-safe)
# ======================
class ScalpBot:
    def __init__(self, cfg: BotConfig, logger:Callable[[str],None]=print, on_trade:Callable[[dict,object],None]=lambda *args:None):
        self.cfg = cfg
        self.logger = logger
        self.on_trade = on_trade
        self.stop_event = threading.Event()
        self.thread = None
        self.last_trade_time = {"buy": datetime.min, "sell": datetime.min}
        self._silent = lambda *_: None

        # ==== NEW: stats for UI heartbeat ====
        self._stats_lock = threading.Lock()
        self.stats = {
            "heartbeat": datetime.min,
            "loops": 0,
            "last_bid": None,
            "last_ask": None,
            "thread_alive": False,
            "last_error": "",
        }

    def snapshot(self) -> dict:
        with self._stats_lock:
            return dict(self.stats)

    def log(self, msg):
        try:
            self.logger(str(msg))
        except:
            pass

    def start(self):
        if self.thread and self.thread.is_alive():
            self.log("⚠️ bot already running")
            return
        if not mt5_connect():
            self.log(f"❌ MT5 init failed: {mt5.last_error()}")
            return
        with _mt5_lock:
            if not mt5.symbol_select(self.cfg.symbol, True):
                self.log(f"❌ select symbol failed: {self.cfg.symbol}")
                mt5_disconnect()
                return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run_loop, daemon=True)
        self.thread.start()
        with self._stats_lock:
            self.stats["thread_alive"] = True
            self.stats["heartbeat"] = datetime.now()
        self.log("▶️ bot started")

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
        with self._stats_lock:
            self.stats["thread_alive"] = False
        mt5_disconnect()
        self.log("⏹ bot stopped")

    def run_loop(self):
        tf = TF_MAP.get(self.cfg.tf_name, mt5.TIMEFRAME_M1)
        try:
            while not self.stop_event.is_set():
                df = get_data(self.cfg.symbol, tf, self.cfg.history_bars)
                if df.empty or len(df) < max(self.cfg.ob_lookback_bars, self.cfg.bos_lookback+3):
                    time.sleep(1); 
                    with self._stats_lock:
                        self.stats["heartbeat"] = datetime.now()
                        self.stats["loops"] += 1
                    continue

                info = mt5.symbol_info(self.cfg.symbol)
                tick = mt5.symbol_info_tick(self.cfg.symbol)
                if not info or not tick:
                    time.sleep(1); 
                    with self._stats_lock:
                        self.stats["heartbeat"] = datetime.now()
                        self.stats["loops"] += 1
                    continue

                if not spread_ok(self.cfg.symbol, self.cfg.spread_max_points):
                    time.sleep(1); 
                    with self._stats_lock:
                        self.stats["heartbeat"] = datetime.now()
                        self.stats["loops"] += 1
                    continue

                point = info.point or 0.01
                bid, ask = tick.bid, tick.ask

                # heartbeat update
                with self._stats_lock:
                    self.stats["heartbeat"] = datetime.now()
                    self.stats["loops"] += 1
                    self.stats["last_bid"] = float(bid)
                    self.stats["last_ask"] = float(ask)
                    self.stats["thread_alive"] = True

                # --- OB Detection ---
                bull_ob, bear_ob = find_order_blocks(df, self.cfg.ob_lookback_bars, self.cfg.bos_lookback)
                in_bull_ob = touch_zone_points(ask, bull_ob, self.cfg.ob_pad_points, point)
                in_bear_ob = touch_zone_points(bid, bear_ob, self.cfg.ob_pad_points, point)

                # --- Confirmation (เดิม) ---
                rej_buy = is_bullish_rejection(df, point, self.cfg) if self.cfg.confirm_use_rejection else False
                rej_sell = is_bearish_rejection(df, point, self.cfg) if self.cfg.confirm_use_rejection else False
                bos_up   = mini_bos_up(df,  self.cfg.bos_confirm_lookback) if self.cfg.confirm_use_bos else False
                bos_dn   = mini_bos_down(df, self.cfg.bos_confirm_lookback) if self.cfg.confirm_use_bos else False

                # ===== SMC Confluences =====
                # 3.1 FVG ล่าสุด + เช็คว่าราคาปัจจุบันแตะ FVG ไหม
                bull_fvg = bear_fvg = None
                in_bull_fvg = in_bear_fvg = False
                if self.cfg.use_fvg:
                    bull_fvg, bear_fvg = detect_fvg(df, self.cfg.fvg_lookback,
                                                    self.cfg.fvg_min_gap_points, point)
                    in_bull_fvg = touch_zone_points(ask, bull_fvg, self.cfg.ob_pad_points, point)
                    in_bear_fvg = touch_zone_points(bid, bear_fvg, self.cfg.ob_pad_points, point)

                # 3.2 Liquidity Sweep / SFP
                bull_sweep = bear_sweep = False
                if self.cfg.use_sweep:
                    bull_sweep, bear_sweep = detect_liquidity_sweep(
                        df, point, self.cfg.sweep_lookback, self.cfg.sweep_tolerance_pts, use_closed_bar=True
                    )

                # 3.3 MACD filter (momentum)
                macd_buy_ok = macd_sell_ok = True
                if self.cfg.use_macd:
                    m, s, hst = macd(df["close"], self.cfg.macd_fast, self.cfg.macd_slow, self.cfg.macd_signal)
                    macd_buy_ok  = (hst.iloc[-1] > 0) and (m.iloc[-1] > s.iloc[-1])
                    macd_sell_ok = (hst.iloc[-1] < 0) and (m.iloc[-1] < s.iloc[-1])

                # ===== รวมสัญญาณเป็น “คะแนนคอนฟลูเอนซ์” =====
                need = max(1, int(self.cfg.confirm_min_score))  # ต้องผ่านกี่เงื่อนไขขั้นต่ำ

                def _enough(need, *conds):
                    return sum(1 for c in conds if bool(c)) >= need

                def confirmed_for_buy():
                    base = (
                        rej_buy,
                        bos_up,
                        (bull_sweep if self.cfg.use_sweep else False),
                        (in_bull_fvg if self.cfg.use_fvg else False),
                    )
                    ok = _enough(need, *base)
                    if self.cfg.use_macd:
                        ok = ok and macd_buy_ok
                    return ok

                def confirmed_for_sell():
                    base = (
                        rej_sell,
                        bos_dn,
                        (bear_sweep if self.cfg.use_sweep else False),
                        (in_bear_fvg if self.cfg.use_fvg else False),
                    )
                    ok = _enough(need, *base)
                    if self.cfg.use_macd:
                        ok = ok and macd_sell_ok
                    return ok

                # --- Limits ---
                poses = mt5.positions_get(symbol=self.cfg.symbol) or []
                now = datetime.now()
                can_buy  = (now - self.last_trade_time["buy"]).total_seconds()  >= self.cfg.cooldown_sec
                can_sell = (now - self.last_trade_time["sell"]).total_seconds() >= self.cfg.cooldown_sec
                have_room= (len(poses) < self.cfg.max_open_pos)

                # stale close / breakeven (เงียบ ไม่ log)
                close_stale_positions(self.cfg.symbol, self.cfg.max_hold_sec, logger=self._silent)
                manage_breakeven_for_symbol(self.cfg, logger=self._silent)

                # SR for adaptive TP (optional)
                s=r=None
                if self.cfg.tp_mode == "ADAPTIVE_TO_ZONE":
                    s, r = self._find_sr_levels(df, self.cfg.sr_lookback)

                # --- Entry rules ---
                if self.cfg.side_mode in ("BOTH","BUY_ONLY") and have_room and can_buy and in_bull_ob and confirmed_for_buy():
                    res, req = send_market_order(self.cfg.symbol, "buy", self.cfg.lot_fixed,
                                                 self.cfg.sl_points_fixed, choose_tp_points(info, ask, "buy", s, r, bull_ob, bear_ob, self.cfg),
                                                 logger=self._silent)
                    if res and getattr(res, "retcode", None) in (RETCODE_DONE, RETCODE_PLACED):
                        self.last_trade_time["buy"] = now
                        self.on_trade(req, res)

                if self.cfg.side_mode in ("BOTH","SELL_ONLY") and have_room and can_sell and in_bear_ob and confirmed_for_sell():
                    res, req = send_market_order(self.cfg.symbol, "sell", self.cfg.lot_fixed,
                                                 self.cfg.sl_points_fixed, choose_tp_points(info, bid, "sell", s, r, bull_ob, bear_ob, self.cfg),
                                                 logger=self._silent)
                    if res and getattr(res, "retcode", None) in (RETCODE_DONE, RETCODE_PLACED):
                        self.last_trade_time["sell"] = now
                        self.on_trade(req, res)

                time.sleep(1)

        except Exception as e:
            with self._stats_lock:
                self.stats["last_error"] = str(e)
            self.log(f"💥 error: {e}\n{traceback.format_exc()}")

    # --- SR (ใช้เฉพาะโหมด TP adaptive) ---
    @staticmethod
    def _is_support(df: pd.DataFrame, i: int) -> bool:
        return (df["low"].iloc[i] < df["low"].iloc[i-1]) and (df["low"].iloc[i] < df["low"].iloc[i+1])

    @staticmethod
    def _is_resistance(df: pd.DataFrame, i: int) -> bool:
        return (df["high"].iloc[i] > df["high"].iloc[i-1]) and (df["high"].iloc[i] > df["high"].iloc[i+1])

    def _find_sr_levels(self, df: pd.DataFrame, lookback: int) -> Tuple[Optional[float], Optional[float]]:
        if len(df) < lookback + 2:
            return None, None
        s_levels = [df["low"].iloc[i]  for i in range(len(df)-lookback, len(df)-1)
                    if 0 < i < len(df)-1 and self._is_support(df, i)]
        r_levels = [df["high"].iloc[i] for i in range(len(df)-lookback, len(df)-1)
                    if 0 < i < len(df)-1 and self._is_resistance(df, i)]
        s = max(s_levels) if s_levels else None
        r = min(r_levels) if r_levels else None
        return s, r


# ======================
# Tkinter GUI + Multi-bot manager + Report
# ======================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MT5 Scalp Bot — OB + Confirmation (Extended) + Trade-Only Log + P&L Report")
        self.geometry("1220x820")

        # general log (UI only; เราจะเขียนเฉพาะตอนเข้าออเดอร์)
        self.log_to_file_var = tk.BooleanVar(value=False)  # ปิด default
        self.log_path_var = tk.StringVar(value=os.path.join(os.getcwd(), "scalp_logs.txt"))

        # trade CSV (บันทึกเฉพาะตอนเข้าออเดอร์)
        self.trade_csv_enable_var = tk.BooleanVar(value=True)
        self.trade_csv_path_var = tk.StringVar(value=os.path.join(os.getcwd(), "trade_log.csv"))
        self._ensure_trade_csv_header()

        # equity series
        self.equity_series = []
        self.equity_times = []

        # after() jobs ids
        self._pos_job = None
        self._eq_job = None

        self.nb = ttk.Notebook(self); self.nb.pack(fill="both", expand=True)
        self.tab_control = ttk.Frame(self.nb); self.nb.add(self.tab_control, text="Control")
        self.tab_bots    = ttk.Frame(self.nb); self.nb.add(self.tab_bots, text="Bots Manager")
        self.tab_pos     = ttk.Frame(self.nb); self.nb.add(self.tab_pos, text="Positions / Equity")
        self.tab_report  = ttk.Frame(self.nb); self.nb.add(self.tab_report, text="P&L Report")

        self._build_control_tab()
        self._build_bots_tab()
        self._build_positions_tab()
        self._build_report_tab()
        self._build_toolbar()

        # schedule + store ids
        self._pos_job = self.after(1000, self._poll_positions)
        if HAVE_MPL:
            self._eq_job = self.after(1500, self._poll_equity)

        self.next_bot_id = 1
        self.bots = {}

        self.report_rows = []  # last generated deals rows

        # ==== NEW: spinner/heartbeat poller ====
        self._spin_chars = ["|", "/", "-", "\\"]
        self._spin_idx = 0
        self._health_job = self.after(500, self._pulse_bots)

        # สร้าง Default set ตามรูป (และ autostart ถ้าตั้ง True)
        if AUTO_CREATE_DEFAULTS_ON_LAUNCH and not self.bots:
            self.create_default_bots(auto_start=AUTO_START_DEFAULTS_ON_LAUNCH)

        # close handler
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- Toolbar ----------
    def _build_toolbar(self):
        bar = ttk.Frame(self); bar.pack(fill="x", padx=8, pady=6)

        ttk.Button(bar, text="Test MT5", command=self.test_mt5).pack(side="left", padx=6)

        # General log (เราไม่ค่อยใช้แล้ว แต่เผื่อไว้)
        ttk.Checkbutton(bar, text="Write UI log to file", variable=self.log_to_file_var).pack(side="left", padx=8)
        ttk.Entry(bar, textvariable=self.log_path_var, width=38).pack(side="left", padx=4)
        ttk.Button(bar, text="Browse…", command=self._pick_general_log).pack(side="left", padx=4)

        # Trade CSV
        ttk.Checkbutton(bar, text="Save Trade Entries (CSV)", variable=self.trade_csv_enable_var).pack(side="left", padx=12)
        ttk.Entry(bar, textvariable=self.trade_csv_path_var, width=28).pack(side="left", padx=4)
        ttk.Button(bar, text="CSV Path…", command=self._pick_trade_csv).pack(side="left", padx=4)

    def _pick_general_log(self):
        path = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text","*.txt"),("All","*.*")])
        if path:
            self.log_path_var.set(path)

    def _pick_trade_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV","*.csv"),("All","*.*")])
        if path:
            self.trade_csv_path_var.set(path)
            self._ensure_trade_csv_header()

    def _ensure_trade_csv_header(self):
        path = self.trade_csv_path_var.get()
        try:
            newfile = not os.path.exists(path)
            if newfile:
                with open(path, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(["time_open","symbol","side","volume","price","sl","tp","order_id","deal_id","magic","comment"])
        except Exception:
            pass

    # ---------- Control (Quick Start) ----------
    def _build_control_tab(self):
        frm = ttk.Frame(self.tab_control); frm.pack(fill="x", padx=8, pady=8)
        self.symbol_var = tk.StringVar(value="GOLD#")
        self.tf_var     = tk.StringVar(value="M1")
        self.lot_var    = tk.DoubleVar(value=0.02)

        ttk.Label(frm, text="Symbol").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.symbol_var, width=12).grid(row=0, column=1, padx=6)

        ttk.Label(frm, text="TF").grid(row=0, column=2, sticky="w")
        ttk.Combobox(frm, textvariable=self.tf_var, values=["M1","M5","M15"], width=6, state="readonly").grid(row=0, column=3, padx=6)

        ttk.Label(frm, text="Lot").grid(row=0, column=4, sticky="w")
        ttk.Entry(frm, textvariable=self.lot_var, width=8).grid(row=0, column=5, padx=6)

        # Row 2: TP/SL + limits
        self.tp_mode_var = tk.StringVar(value="FIXED")
        self.tp_fixed_var = tk.IntVar(value=300)
        self.sl_fixed_var = tk.IntVar(value=150)
        self.tp_min_var = tk.IntVar(value=100)
        self.tp_max_var = tk.IntVar(value=300)

        self.spread_var = tk.IntVar(value=120)
        self.cooldown_var = tk.IntVar(value=30)
        self.maxpos_var = tk.IntVar(value=5)
        self.hold_var = tk.IntVar(value=900)

        row = 1
        ttk.Label(frm, text="TP mode").grid(row=row, column=0, sticky="w")
        ttk.Combobox(frm, textvariable=self.tp_mode_var, values=["FIXED","RANGE_RANDOM","ADAPTIVE_TO_ZONE"], width=16,
                     state="readonly").grid(row=row, column=1, padx=6)
        ttk.Label(frm, text="TP fixed").grid(row=row, column=2, sticky="w")
        ttk.Entry(frm, textvariable=self.tp_fixed_var, width=8).grid(row=row, column=3, padx=6)
        ttk.Label(frm, text="SL fixed").grid(row=row, column=4, sticky="w")
        ttk.Entry(frm, textvariable=self.sl_fixed_var, width=8).grid(row=row, column=5, padx=6)
        ttk.Label(frm, text="TP min/max").grid(row=row, column=6, sticky="w")
        tk.Entry(frm, textvariable=self.tp_min_var, width=6).grid(row=row, column=7, padx=2, sticky="w")
        tk.Entry(frm, textvariable=self.tp_max_var, width=6).grid(row=row, column=8, padx=2, sticky="w")

        row += 1
        ttk.Label(frm, text="Max Spread").grid(row=row, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.spread_var, width=8).grid(row=row, column=1, padx=6)
        ttk.Label(frm, text="Cooldown (s)").grid(row=row, column=2, sticky="w")
        ttk.Entry(frm, textvariable=self.cooldown_var, width=8).grid(row=row, column=3, padx=6)
        ttk.Label(frm, text="Max Positions").grid(row=row, column=4, sticky="w")
        ttk.Entry(frm, textvariable=self.maxpos_var, width=8).grid(row=row, column=5, padx=6)
        ttk.Label(frm, text="Max Hold (s)").grid(row=row, column=6, sticky="w")
        ttk.Entry(frm, textvariable=self.hold_var, width=8).grid(row=row, column=7, padx=6)

        # Row 3: confirmations + side
        self.rej_var = tk.BooleanVar(value=True)
        self.bos_var = tk.BooleanVar(value=True)
        self.confirm_mode_var = tk.StringVar(value="ANY")
        self.side_mode_var = tk.StringVar(value="BOTH")

        ttk.Checkbutton(frm, text="Use Rejection", variable=self.rej_var).grid(row=row+1, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(frm, text="Use mini BOS", variable=self.bos_var).grid(row=row+1, column=2, columnspan=2, sticky="w")
        ttk.Label(frm, text="Confirm Mode").grid(row=row+1, column=4, sticky="w")
        ttk.Combobox(frm, textvariable=self.confirm_mode_var, values=["ANY","BOTH"], width=6, state="readonly").grid(row=row+1, column=5, padx=6)
        ttk.Label(frm, text="Side Mode").grid(row=row+1, column=6, sticky="w")
        ttk.Combobox(frm, textvariable=self.side_mode_var, values=["BOTH","BUY_ONLY","SELL_ONLY"], width=10, state="readonly").grid(row=row+1, column=7, padx=6)

        # === Breakeven controls ===
        be_row = row + 2
        self.be_enable_var  = tk.BooleanVar(value=True)
        self.be_trigger_var = tk.IntVar(value=100)
        self.be_offset_var  = tk.IntVar(value=5)

        ttk.Checkbutton(frm, text="Enable Breakeven", variable=self.be_enable_var)\
        .grid(row=be_row, column=0, columnspan=2, sticky="w")

        ttk.Label(frm, text="BE trigger (pts)")\
        .grid(row=be_row, column=2, sticky="w")
        ttk.Entry(frm, textvariable=self.be_trigger_var, width=8)\
        .grid(row=be_row, column=3, padx=6)

        ttk.Label(frm, text="BE offset (pts)")\
        .grid(row=be_row, column=4, sticky="w")
        ttk.Entry(frm, textvariable=self.be_offset_var, width=8)\
        .grid(row=be_row, column=5, padx=6)

        # Buttons
        btnfrm = ttk.Frame(self.tab_control); btnfrm.pack(fill="x", padx=8, pady=4)
        self.start_btn = ttk.Button(btnfrm, text="▶️ Quick Start", command=self.on_quick_start); self.start_btn.pack(side="left", padx=4)
        self.stop_btn  = ttk.Button(btnfrm, text="⏹ Stop All",  command=self.on_stop_all); self.stop_btn.pack(side="left", padx=4)

        # Logs (จะแสดงเฉพาะข้อความตอนเข้าออเดอร์)
        self.logbox = ScrolledText(self.tab_control, height=12)
        self.logbox.pack(fill="both", expand=True, padx=8, pady=8)

    # ---------- Bots Manager ----------
    def _build_bots_tab(self):
        top = ttk.Frame(self.tab_bots); top.pack(fill="x", padx=8, pady=8)
        ttk.Button(top, text="➕ Add Bot from Control Form", command=self.on_add_bot).pack(side="left", padx=4)
        ttk.Button(top, text="⭐ Add Default Set", command=lambda: self.create_default_bots(auto_start=False)).pack(side="left", padx=4)
        ttk.Button(top, text="▶️ Start All", command=self.on_start_all).pack(side="left", padx=4)
        ttk.Button(top, text="▶️ Start Selected", command=self.on_start_selected).pack(side="left", padx=4)
        ttk.Button(top, text="⏹ Stop Selected", command=self.on_stop_selected).pack(side="left", padx=4)
        ttk.Button(top, text="🗑 Remove", command=self.on_remove_selected).pack(side="left", padx=4)

        cols = ("id","symbol","tf","lot","side","tp_mode","tp/sl","spread","cooldown","maxpos","status")
        self.tree = ttk.Treeview(self.tab_bots, columns=cols, show="headings", height=10)
        for c in cols: self.tree.heading(c, text=c.upper())
        self.tree.column("id", width=50); self.tree.column("symbol", width=90)
        self.tree.column("tf", width=50);  self.tree.column("lot", width=70)
        self.tree.column("side", width=90);self.tree.column("tp_mode", width=120)
        self.tree.column("tp/sl", width=110);self.tree.column("spread", width=90)
        self.tree.column("cooldown", width=90);self.tree.column("maxpos", width=80)
        self.tree.column("status", width=140)
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)

        # NEW: row highlight tags
        self.tree.tag_configure("warn", background="#FFF3CD")  # เหลืองอ่อน
        self.tree.tag_configure("ok", background="")           # ปกติ

    def _add_bot_with_cfg(self, cfg: BotConfig, auto_start: bool = False):
        bid = self.next_bot_id; self.next_bot_id += 1
        bot = ScalpBot(
            cfg,
            logger=lambda m, _bid=bid: None,  # เงียบ UI ทั่วไป
            on_trade=lambda req, res, _bid=bid: self.record_trade_open(req, res, prefix=f"[Bot#{_bid} {cfg.symbol}] ")
        )
        self.bots[bid] = (bot, cfg)
        status = "Running" if auto_start else "Stopped"
        self.tree.insert("", "end", iid=str(bid), values=(
            bid, cfg.symbol, cfg.tf_name, cfg.lot_fixed, cfg.side_mode, cfg.tp_mode,
            f"{cfg.tp_points_fixed}/{cfg.sl_points_fixed}", cfg.spread_max_points,
            cfg.cooldown_sec, cfg.max_open_pos, status
        ))
        if auto_start:
            bot.start()

    def create_default_bots(self, auto_start: bool = False):
        # ป้องกันซ้ำง่ายๆ: ถ้ามีบอทอยู่แล้วก็ไม่สร้างซ้ำ
        if self.bots:
            self.log("Default set: มีบอทอยู่แล้ว จึงไม่สร้างซ้ำ (กด Remove ก่อนถ้าต้องการชุดใหม่)")
            return
        for mode in DEFAULT_TPMODES:
            for tf in DEFAULT_TFS:
                cfg = BotConfig(
                    symbol=DEFAULT_SYMBOL,
                    tf_name=tf,
                    lot_fixed=DEFAULT_LOT,
                    tp_mode=mode,
                    tp_points_fixed=DEFAULT_TP,
                    sl_points_fixed=DEFAULT_SL,
                    tp_points_min=100,
                    tp_points_max=300,
                    spread_max_points=DEFAULT_SPREAD,
                    cooldown_sec=DEFAULT_COOLDOWN,
                    max_open_pos=DEFAULT_MAXPOS,
                    max_hold_sec=DEFAULT_MAXHOLD,
                    confirm_use_rejection=True,
                    confirm_use_bos=True,
                    confirm_mode="ANY",
                    side_mode=DEFAULT_SIDE,
                    be_enable=True,
                    be_trigger_points=100,
                    be_offset_points=5,
                )
                self._add_bot_with_cfg(cfg, auto_start=auto_start)
        self.log(f"⭐ Default set created ({'Running' if auto_start else 'Stopped'})")

    def on_add_bot(self):
        cfg = self._cfg_from_form()
        bid = self.next_bot_id; self.next_bot_id += 1
        bot = ScalpBot(cfg,
                    logger=lambda m, _bid=bid: None,  # เงียบ
                    on_trade=lambda req, res, _bid=bid: self.record_trade_open(req, res, prefix=f"[Bot#{_bid} {cfg.symbol}] "))
        self.bots[bid] = (bot, cfg)
        self.tree.insert("", "end", iid=str(bid), values=(
            bid, cfg.symbol, cfg.tf_name, cfg.lot_fixed, cfg.side_mode, cfg.tp_mode,
            f"{cfg.tp_points_fixed}/{cfg.sl_points_fixed}", cfg.spread_max_points,
            cfg.cooldown_sec, cfg.max_open_pos, "Stopped"
        ))

    def _selected_bot_ids(self) -> List[int]:
        ids = []
        for sel in self.tree.selection():
            try: ids.append(int(sel))
            except: pass
        return ids
    
    def on_start_all(self):
        if not self.bots:
            self.log("ไม่มีบอทในรายการ")
            return
        started = []
        for bid, (bot, cfg) in self.bots.items():
            bot.start()  # ถ้ารันอยู่แล้ว start() จะไม่เริ่มซ้ำ
            try:
                self.tree.set(str(bid), "status", "Running")
            except Exception:
                pass
            started.append(bid)
        self.log(f"▶️ Start All: {started}")

    def on_start_selected(self):
        for bid in self._selected_bot_ids():
            bot, cfg = self.bots.get(bid, (None, None))
            if not bot: continue
            bot.start()
            self.tree.set(str(bid), "status", "Running")

    def on_stop_selected(self):
        for bid in self._selected_bot_ids():
            bot, cfg = self.bots.get(bid, (None, None))
            if not bot: continue
            bot.stop()
            self.tree.set(str(bid), "status", "Stopped")

    def on_remove_selected(self):
        for bid in self._selected_bot_ids():
            bot, cfg = self.bots.pop(bid, (None, None))
            if bot:
                try: bot.stop()
                except: pass
            try: self.tree.delete(str(bid))
            except: pass

    # ==== NEW: heartbeat updater ====
    def _pulse_bots(self):
        self._spin_idx = (self._spin_idx + 1) % len(self._spin_chars)
        ch = self._spin_chars[self._spin_idx]
        now = datetime.now()

        for bid, (bot, cfg) in list(self.bots.items()):
            item_id = str(bid)
            status_text = "Stopped"
            row_tag = ("ok",)

            try:
                alive = bot.thread.is_alive() if bot.thread else False
                st = bot.snapshot()
                if alive and st:
                    age = (now - st["heartbeat"]).total_seconds()
                    if age <= 4.0:
                        status_text = f"Running {ch} {age:0.1f}s"
                        row_tag = ("ok",)
                    else:
                        status_text = f"Stalled ⚠ {age:0.1f}s"
                        row_tag = ("warn",)
                else:
                    status_text = "Stopped"
            except Exception:
                status_text = "N/A"

            try:
                self.tree.set(item_id, "status", status_text)
                self.tree.item(item_id, tags=row_tag)
            except Exception:
                pass

        self._health_job = self.after(500, self._pulse_bots)

    # ---------- Positions / Equity ----------
    def _build_positions_tab(self):
        upper = ttk.Frame(self.tab_pos); upper.pack(fill="x", padx=8, pady=8)
        self.pos_symbol_filter = tk.StringVar(value="")
        ttk.Label(upper, text="Filter Symbol:").pack(side="left")
        ttk.Entry(upper, textvariable=self.pos_symbol_filter, width=12).pack(side="left", padx=4)
        ttk.Button(upper, text="Refresh", command=self._update_positions_view).pack(side="left", padx=4)
        self.auto_pos_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(upper, text="Auto refresh", variable=self.auto_pos_var).pack(side="left", padx=10)

        cols = ("ticket","symbol","type","volume","price_open","sl","tp","profit","time")
        self.pos_tree = ttk.Treeview(self.tab_pos, columns=cols, show="headings", height=12)
        for c in cols: self.pos_tree.heading(c, text=c.upper())
        self.pos_tree.column("ticket", width=90); self.pos_tree.column("symbol", width=90)
        self.pos_tree.column("type", width=70);   self.pos_tree.column("volume", width=80)
        self.pos_tree.column("price_open", width=100); self.pos_tree.column("sl", width=100)
        self.pos_tree.column("tp", width=100); self.pos_tree.column("profit", width=90)
        self.pos_tree.column("time", width=150)
        self.pos_tree.pack(fill="both", expand=True, padx=8, pady=8)

        if HAVE_MPL:
            self.fig = Figure(figsize=(7,2.8), dpi=100)
            self.ax = self.fig.add_subplot(111)
            self.ax.set_title("Account Equity (live)")
            self.ax.set_xlabel("Time")
            self.ax.set_ylabel("Equity")
            self.canvas = FigureCanvasTkAgg(self.fig, master=self.tab_pos)
            self.canvas.get_tk_widget().pack(fill="x", padx=8, pady=8)

    def _update_positions_view(self):
        for i in self.pos_tree.get_children():
            self.pos_tree.delete(i)
        ok = mt5_connect()
        if not ok: return
        try:
            filt = self.pos_symbol_filter.get().strip().upper()
            poses = mt5.positions_get() or []
            for p in poses:
                sym = p.symbol.upper()
                if filt and filt not in sym: continue
                typ = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
                tstr = datetime.fromtimestamp(int(p.time)).strftime("%Y-%m-%d %H:%M:%S")
                self.pos_tree.insert("", "end", values=(
                    p.ticket, p.symbol, typ, p.volume, round(p.price_open,2), round(p.sl,2), round(p.tp,2),
                    round(p.profit,2), tstr
                ))
        finally:
            mt5_disconnect()

    def _poll_positions(self):
        if self.auto_pos_var.get():
            self._update_positions_view()
        self._pos_job = self.after(1500, self._poll_positions)

    def _poll_equity(self):
        ok = mt5_connect()
        if ok:
            try:
                ai = mt5.account_info()
                if ai:
                    self.equity_series.append(float(ai.equity))
                    self.equity_times.append(datetime.now())
                    if HAVE_MPL:
                        self.ax.clear()
                        self.ax.set_title("Account Equity (live)")
                        self.ax.plot(self.equity_times[-300:], self.equity_series[-300:])
                        self.fig.autofmt_xdate()
                        self.canvas.draw()
            finally:
                mt5_disconnect()
        if HAVE_MPL:
            self._eq_job = self.after(2000, self._poll_equity)

    # ---------- Report Tab ----------
    def _build_report_tab(self):
        top = ttk.Frame(self.tab_report); top.pack(fill="x", padx=8, pady=8)
        ttk.Label(top, text="Days back:").pack(side="left")
        self.report_days_var = tk.IntVar(value=7)
        ttk.Entry(top, textvariable=self.report_days_var, width=6).pack(side="left", padx=4)
        ttk.Label(top, text="Symbol filter:").pack(side="left", padx=8)
        self.report_symbol_var = tk.StringVar(value="")
        ttk.Entry(top, textvariable=self.report_symbol_var, width=12).pack(side="left", padx=4)
        ttk.Button(top, text="Generate", command=self.on_generate_report).pack(side="left", padx=8)
        ttk.Button(top, text="Export CSV", command=self.on_export_report).pack(side="left", padx=4)

        self.summary_var = tk.StringVar(value="—")
        ttk.Label(self.tab_report, textvariable=self.summary_var, anchor="w").pack(fill="x", padx=10, pady=(0,6))

        cols = ("time","symbol","side","price","volume","profit","commission","swap","ticket","order","comment")
        self.rep_tree = ttk.Treeview(self.tab_report, columns=cols, show="headings", height=16)
        for c in cols: self.rep_tree.heading(c, text=c.upper())
        self.rep_tree.column("time", width=150); self.rep_tree.column("symbol", width=90)
        self.rep_tree.column("side", width=60);  self.rep_tree.column("price", width=80)
        self.rep_tree.column("volume", width=70);self.rep_tree.column("profit", width=80)
        self.rep_tree.column("commission", width=90); self.rep_tree.column("swap", width=70)
        self.rep_tree.column("ticket", width=90); self.rep_tree.column("order", width=90)
        self.rep_tree.column("comment", width=180)
        self.rep_tree.pack(fill="both", expand=True, padx=8, pady=8)

    def on_generate_report(self):
        for i in self.rep_tree.get_children():
            self.rep_tree.delete(i)
        self.report_rows = []
        days = max(1, int(self.report_days_var.get()))
        symf = self.report_symbol_var.get().strip().upper()

        ok = mt5_connect()
        if not ok:
            self.summary_var.set("Cannot connect MT5")
            return
        try:
            date_from = datetime.now() - timedelta(days=days)
            date_to   = datetime.now() + timedelta(seconds=5)
            deals = mt5.history_deals_get(date_from, date_to) or []
            wins = losses = draws = 0
            gp = gl = 0.0

            for d in deals:
                # filter magic + entry out
                if getattr(d, "magic", 0) != BOT_MAGIC:
                    continue
                if getattr(d, "entry", 0) != DEAL_ENTRY_OUT:
                    continue
                sym = d.symbol.upper()
                if symf and symf not in sym:
                    continue
                side = "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL"
                net  = float(d.profit) + float(getattr(d, "swap", 0.0)) + float(getattr(d, "commission", 0.0))
                if net > 0: wins += 1; gp += net
                elif net < 0: losses += 1; gl += net
                else: draws += 1
                tstr = datetime.fromtimestamp(int(d.time)).strftime("%Y-%m-%d %H:%M:%S")
                row = (tstr, sym, side, round(float(d.price), 2), float(d.volume),
                       round(float(d.profit),2), round(float(getattr(d,"commission",0.0)),2),
                       round(float(getattr(d,"swap",0.0)),2), d.ticket, d.order, d.comment)
                self.rep_tree.insert("", "end", values=row)
                self.report_rows.append(row)

            total = wins + losses + draws
            net_total = gp + gl  # gl negative
            winrate = (wins / (wins + losses) * 100.0) if (wins+losses)>0 else 0.0
            summary = f"Trades: {total} | Wins: {wins} Losses: {losses} Draws: {draws} | Win%: {winrate:.2f}% | " \
                      f"Gross P: {gp:.2f}  Gross L: {gl:.2f}  Net: {net_total:.2f}"
            self.summary_var.set(summary)
        finally:
            mt5_disconnect()

    def on_export_report(self):
        if not self.report_rows:
            self.summary_var.set("No data to export.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV","*.csv"),("All","*.*")],
                                            initialfile="pnl_report.csv")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["time","symbol","side","price","volume","profit","commission","swap","ticket","order","comment"])
                w.writerows(self.report_rows)
            self.summary_var.set(f"Exported: {path}")
        except Exception as e:
            self.summary_var.set(f"Export failed: {e}")

    # ---------- MT5 test ----------
    def test_mt5(self):
        ok = mt5_connect()
        if not ok:
            self.log("init failed: {}".format(mt5.last_error())); return
        try:
            sym = self.symbol_var.get()
            if not mt5.symbol_select(sym, True):
                self.log(f"select symbol failed: {sym}")
            else:
                info = mt5.symbol_info(sym)
                self.log(f"OK {sym} digits={info.digits} point={info.point} spread={info.spread}")
        finally:
            mt5_disconnect()

    # ---------- Logging ----------
    def log(self, text: str):
        """UI log: ใช้เฉพาะข้อความสำคัญ (เข้าออเดอร์/แจ้งเตือนทั่วไป)"""
        def _append():
            ts = datetime.now().strftime("%H:%M:%S")
            line = f"{ts} | {text}"
            try:
                self.logbox.insert("end", line + "\n")
                self.logbox.see("end")
                if self.log_to_file_var.get():
                    with open(self.log_path_var.get(), "a", encoding="utf-8") as f:
                        f.write(line + "\n")
            except Exception:
                pass
        try:
            self.logbox.after(0, _append)
        except Exception:
            pass

    def record_trade_open(self, req: dict, res: object, prefix: str = ""):
        """บันทึกเฉพาะตอน 'เข้าออเดอร์' → UI + CSV"""
        side = "BUY" if req["type"] == mt5.ORDER_TYPE_BUY else "SELL"
        symbol = req["symbol"]
        vol = req["volume"]
        price = getattr(res, "price", 0.0) or req["price"]
        sl = req.get("sl", 0.0); tp = req.get("tp", 0.0)
        order_id = getattr(res, "order", 0)
        deal_id  = getattr(res, "deal", 0)
        msg = f"{prefix}OPEN {side} {symbol} vol={vol} @ {price:.2f} | SL={sl:.2f} TP={tp:.2f} | order={order_id} deal={deal_id}"
        self.log(msg)

        if self.trade_csv_enable_var.get():
            try:
                with open(self.trade_csv_path_var.get(), "a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow([
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        symbol, side, vol, round(price,2), round(sl,2), round(tp,2),
                        order_id, deal_id, BOT_MAGIC, req.get("comment","")
                    ])
            except Exception as e:
                self.log(f"❌ write trade csv failed: {e}")

    # ---------- Quick Start ----------
    def _cfg_from_form(self) -> BotConfig:
        return BotConfig(
            symbol=self.symbol_var.get().strip(),
            tf_name=self.tf_var.get(),
            lot_fixed=float(self.lot_var.get()),
            tp_mode=self.tp_mode_var.get(),
            tp_points_fixed=int(self.tp_fixed_var.get()),
            sl_points_fixed=int(self.sl_fixed_var.get()),
            tp_points_min=int(self.tp_min_var.get()),
            tp_points_max=int(self.tp_max_var.get()),
            spread_max_points=int(self.spread_var.get()),
            cooldown_sec=int(self.cooldown_var.get()),
            max_open_pos=int(self.maxpos_var.get()),
            max_hold_sec=int(self.hold_var.get()),
            confirm_use_rejection=bool(self.rej_var.get()),
            confirm_use_bos=bool(self.bos_var.get()),
            confirm_mode=self.confirm_mode_var.get(),
            side_mode=self.side_mode_var.get(),
            be_enable=bool(self.be_enable_var.get()),
            be_trigger_points=int(self.be_trigger_var.get()),
            be_offset_points=int(self.be_offset_var.get()),
        )

    def on_quick_start(self):
        cfg = self._cfg_from_form()
        bot = ScalpBot(cfg,
                       logger=lambda m: None,  # เงียบ
                       on_trade=lambda req, res: self.record_trade_open(req, res, prefix="[Quick] "))
        bid = f"quick-{int(time.time())}"
        self.bots[bid] = (bot, cfg)
        bot.start()

    def on_stop_all(self):
        for bid, (bot, cfg) in list(self.bots.items()):
            try: bot.stop()
            except: pass
        self.log("⏹ all bots stopped")

    # ---------- Graceful close ----------
    def on_close(self):
        for _, (bot, _) in list(self.bots.items()):
            try: bot.stop()
            except: pass
        try:
            if self._pos_job is not None:
                self.after_cancel(self._pos_job)
        except: pass
        try:
            if self._eq_job is not None:
                self.after_cancel(self._eq_job)
        except: pass
        try:
            self.destroy()
        except:
            os._exit(0)

    # (unused)
    def _poll(self):
        self.after(500, self._poll)


if __name__ == "__main__":
    app = App()
    app.mainloop()
