import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import threading
import time
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional, Tuple

# ====== Tkinter GUI ======
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

"""
Windows App: Scalp Bot (Order Block + Confirmation)
- เลือก TF (M1/M5/M15), Symbol, Lot, โหมดฝั่ง (Buy-only / Sell-only / Both)
- เข้าเฉพาะ "แตะ OB" + "ยืนยัน" (rejection candle หรือ mini BOS) ตามที่กำหนด
- TP 300 / SL 150 (ปรับได้ใน UI)
- กรองสเปรด, คูลดาวน์, จำกัดจำนวนโพซิชัน, ปิดโพซิชันค้างนาน
- ส่งคำสั่งแบบลองหลาย filling mode (FOK→IOC→RETURN→DEFAULT)
- แสดง Live Logs
"""

# ======================
# Strategy Config
# ======================
@dataclass
class BotConfig:
    symbol: str = "GOLD#"
    tf_name: str = "M1"  # "M1" | "M5" | "M15"
    history_bars: int = 1200

    lot_fixed: float = 0.02

    tp_mode: str = "FIXED"   # "FIXED" | "RANGE_RANDOM" | "ADAPTIVE_TO_ZONE"
    tp_points_fixed: int = 300
    sl_points_fixed: int = 150
    tp_points_min: int = 100
    tp_points_max: int = 300

    spread_max_points: int = 120
    cooldown_sec: int = 30
    max_open_pos: int = 5
    max_hold_sec: int = 900     # 0 = off

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


TF_MAP = {
    "M1":  mt5.TIMEFRAME_M1,
    "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
}

FILLING_TRY_ORDER = [
    mt5.ORDER_FILLING_FOK,
    mt5.ORDER_FILLING_IOC,
    mt5.ORDER_FILLING_RETURN,
    None,   # DEFAULT (ไม่ส่ง type_filling)
]

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


def choose_tp_points(info,
                     price_now: float,
                     side: str,
                     s: Optional[float],
                     r: Optional[float],
                     bull_ob: Optional[Tuple[float,float]],
                     bear_ob: Optional[Tuple[float,float]],
                     cfg: BotConfig = None) -> int:
    """คืน TP distance (points) ตามโหมด + เคารพ stops_level"""
    if cfg is None:
        # fallback
        stops_level_pts = info.trade_stops_level or 0
        return max(300, stops_level_pts+1)

    stops_level_pts = info.trade_stops_level or 0
    if cfg.tp_mode == "FIXED":
        tp_pts = cfg.tp_points_fixed
    elif cfg.tp_mode == "RANGE_RANDOM":
        tp_pts = int(np.random.randint(cfg.tp_points_min, cfg.tp_points_max+1))
    else:  # ADAPTIVE_TO_ZONE
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


# ======================
# OB + Confirmation
# ======================
def find_order_blocks(df: pd.DataFrame,
                      lookback: int,
                      bos_back: int) -> Tuple[Optional[Tuple[float,float]], Optional[Tuple[float,float]]]:
    """Bull/Bear OB (วิธีง่าย): มี BOS แล้วหาแท่ง opposite ล่าสุดก่อนหน้าเป็นโซน"""
    if len(df) < max(lookback, bos_back+3):
        return None, None
    t = df.tail(lookback).reset_index(drop=True)
    bull_ob = None
    bear_ob = None
    for i in range(bos_back+3, len(t)):
        win = t.iloc[i-bos_back:i]
        # BOS ขึ้น
        prev_high = win["high"].max()
        if t["close"].iloc[i] > prev_high:
            j = i-1
            while j >= 1 and not (t["close"].iloc[j] < t["open"].iloc[j]):  # แท่งแดงล่าสุดก่อน BOS
                j -= 1
            if j >= 1:
                lo = float(min(t["open"].iloc[j], t["close"].iloc[j], t["low"].iloc[j]))
                hi = float(max(t["open"].iloc[j], t["close"].iloc[j]))
                bull_ob = (lo, hi)
        # BOS ลง
        prev_low = win["low"].min()
        if t["close"].iloc[i] < prev_low:
            j = i-1
            while j >= 1 and not (t["close"].iloc[j] > t["open"].iloc[j]):  # แท่งเขียวล่าสุดก่อน BOS
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
    # pts
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


# ======================
# Order Send (multi filling)
# ======================
def filling_name(m):
    return {mt5.ORDER_FILLING_FOK: "FOK",
            mt5.ORDER_FILLING_IOC: "IOC",
            mt5.ORDER_FILLING_RETURN: "RETURN",
            None: "DEFAULT"}.get(m, str(m))


def _order_send_try_fillings(req_base: dict, logger=print):
    last_res = None
    for mode in FILLING_TRY_ORDER:
        req = req_base.copy()
        if mode is None:
            req.pop("type_filling", None)
        else:
            req["type_filling"] = mode
        res = mt5.order_send(req)
        logger(f"📤 send filling={filling_name(mode)} → {res}")
        last_res = res
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            return res
    return last_res


def send_market_order(symbol: str, side: str, lot: float, sl_pts: int, tp_pts: int, logger=print):
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    if not tick or not info:
        logger("❌ no tick/info")
        return None
    point = info.point or 0.01
    price = tick.ask if side=="buy" else tick.bid
    price = round_price(symbol, price)
    mind = (info.trade_stops_level or 0) * point
    if side=="buy":
        tp = price + max(tp_pts*point, mind+point)
        sl = price - max(sl_pts*point, mind+point)
        otype = mt5.ORDER_TYPE_BUY
    else:
        tp = price - max(tp_pts*point, mind+point)
        sl = price + max(sl_pts*point, mind+point)
        otype = mt5.ORDER_TYPE_SELL
    tp = round_price(symbol, tp); sl = round_price(symbol, sl)
    lot = normalize_volume(symbol, lot)
    deviation = max(10, int((info.spread or 0)*2))
    req = {"action": mt5.TRADE_ACTION_DEAL, "symbol":symbol, "volume":float(lot),
           "type":otype, "price":price, "sl":sl, "tp":tp, "deviation":deviation,
           "magic":246810, "comment":f"Scalp SL{sl_pts} TP{tp_pts}", "type_time":mt5.ORDER_TIME_GTC}
    return _order_send_try_fillings(req, logger)


def close_stale_positions(symbol: str, max_age_sec: int, logger=print):
    if max_age_sec <= 0: return
    now = datetime.now()
    poses = mt5.positions_get(symbol=symbol) or []
    for p in poses:
        opened = datetime.fromtimestamp(int(p.time))
        age = (now - opened).total_seconds()
        if age >= max_age_sec:
            tick = mt5.symbol_info_tick(symbol)
            info = mt5.symbol_info(symbol)
            if not tick or not info:
                continue
            close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
            close_price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask
            close_price = round_price(symbol, close_price)
            deviation = max(10, int((info.spread or 0) * 2))
            req = {"action": mt5.TRADE_ACTION_DEAL, "symbol":symbol, "volume":float(p.volume),
                   "type":close_type, "price":close_price, "deviation":deviation,
                   "magic":246810, "comment":"Stale close", "type_time":mt5.ORDER_TIME_GTC}
            res = _order_send_try_fillings(req, logger)
            logger(f"⏰ stale close ticket={p.ticket} ret={getattr(res,'retcode',None)}")


# ======================
# Bot Runner (thread-safe)
# ======================
class ScalpBot:
    def __init__(self, cfg: BotConfig, logger=print):
        self.cfg = cfg
        self.logger = logger
        self.stop_event = threading.Event()
        self.thread = None
        self.last_trade_time = {"buy": datetime.min, "sell": datetime.min}

    def log(self, msg):
        try:
            self.logger(str(msg))
        except:
            print(msg)

    def start(self):
        if self.thread and self.thread.is_alive():
            self.log("⚠️ bot already running")
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run_loop, daemon=True)
        self.thread.start()
        self.log("▶️ bot started")

    def stop(self):
        self.stop_event.set()
        self.log("🛑 stopping...")
        if self.thread:
            self.thread.join(timeout=3)
        try:
            mt5.shutdown()
        except:
            pass
        self.log("🔌 MT5 shutdown (if initialized)")

    def run_loop(self):
        # MT5 init in thread
        if not mt5.initialize():
            self.log(f"❌ init failed: {mt5.last_error()}")
            return
        if not mt5.symbol_select(self.cfg.symbol, True):
            self.log(f"❌ select symbol failed: {self.cfg.symbol}")
            mt5.shutdown(); return

        tf = TF_MAP.get(self.cfg.tf_name, mt5.TIMEFRAME_M1)
        try:
            while not self.stop_event.is_set():
                df = get_data(self.cfg.symbol, tf, self.cfg.history_bars)
                if df.empty or len(df) < max(self.cfg.ob_lookback_bars, self.cfg.bos_lookback+3):
                    self.log("⏳ no/insufficient data"); time.sleep(1); continue

                info = mt5.symbol_info(self.cfg.symbol)
                tick = mt5.symbol_info_tick(self.cfg.symbol)
                if not info or not tick:
                    time.sleep(1); continue

                if not spread_ok(self.cfg.symbol, self.cfg.spread_max_points):
                    self.log("⛔ spread too wide"); time.sleep(1); continue

                point = info.point or 0.01
                bid, ask = tick.bid, tick.ask

                # --- OB Detection ---
                bull_ob, bear_ob = find_order_blocks(df, self.cfg.ob_lookback_bars, self.cfg.bos_lookback)
                in_bull_ob = touch_zone_points(ask, bull_ob, self.cfg.ob_pad_points, point)
                in_bear_ob = touch_zone_points(bid, bear_ob, self.cfg.ob_pad_points, point)

                # --- Confirmation ---
                rej_buy = is_bullish_rejection(df, point, self.cfg) if self.cfg.confirm_use_rejection else False
                rej_sell= is_bearish_rejection(df, point, self.cfg) if self.cfg.confirm_use_rejection else False
                bos_up  = mini_bos_up(df, self.cfg.bos_confirm_lookback) if self.cfg.confirm_use_bos else False
                bos_dn  = mini_bos_down(df, self.cfg.bos_confirm_lookback) if self.cfg.confirm_use_bos else False

                def confirmed_for_buy():
                    c_rej, c_bos = rej_buy, bos_up
                    return ((c_rej or c_bos) if self.cfg.confirm_mode=="ANY" else (c_rej and c_bos))
                def confirmed_for_sell():
                    c_rej, c_bos = rej_sell, bos_dn
                    return ((c_rej or c_bos) if self.cfg.confirm_mode=="ANY" else (c_rej and c_bos))

                # --- Limits ---
                poses = mt5.positions_get(symbol=self.cfg.symbol) or []
                now = datetime.now()
                can_buy  = (now - self.last_trade_time["buy"]).total_seconds()  >= self.cfg.cooldown_sec
                can_sell = (now - self.last_trade_time["sell"]).total_seconds() >= self.cfg.cooldown_sec
                have_room= (len(poses) < self.cfg.max_open_pos)

                # stale close
                close_stale_positions(self.cfg.symbol, self.cfg.max_hold_sec, self.log)

                # SR for adaptive TP (optional)
                s=r=None
                if self.cfg.tp_mode == "ADAPTIVE_TO_ZONE":
                    s, r = self._find_sr_levels(df, self.cfg.sr_lookback)

                # --- Entry rules ---
                # BUY
                if self.cfg.side_mode in ("BOTH","BUY_ONLY") and have_room and can_buy and in_bull_ob and confirmed_for_buy():
                    tp_pts = choose_tp_points(info, ask, "buy", s, r, bull_ob, bear_ob, self.cfg)
                    res = send_market_order(self.cfg.symbol, "buy", self.cfg.lot_fixed, self.cfg.sl_points_fixed, tp_pts, self.log)
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        self.last_trade_time["buy"] = now

                # SELL
                if self.cfg.side_mode in ("BOTH","SELL_ONLY") and have_room and can_sell and in_bear_ob and confirmed_for_sell():
                    tp_pts = choose_tp_points(info, bid, "sell", s, r, bull_ob, bear_ob, self.cfg)
                    res = send_market_order(self.cfg.symbol, "sell", self.cfg.lot_fixed, self.cfg.sl_points_fixed, tp_pts, self.log)
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        self.last_trade_time["sell"] = now

                # log heartbeat
                self.log(
                    f"[{now.strftime('%H:%M:%S')}] {self.cfg.symbol} {self.cfg.tf_name}  "
                    f"bid:{bid:.2f} ask:{ask:.2f}  "
                    f"touchOB(B/S):{in_bull_ob}/{in_bear_ob}  rej(B/S):{rej_buy}/{rej_sell}  "
                    f"BOS(up/dn):{bos_up}/{bos_dn}  open:{len(poses)}"
                )

                time.sleep(1)

        except Exception as e:
            self.log(f"💥 error: {e}")
        finally:
            mt5.shutdown()
            self.log("🔌 MT5 shutdown")

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
# Tkinter GUI
# ======================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MT5 Scalp Bot — OB + Confirmation")
        self.geometry("880x600")

        # Top form
        frm = ttk.Frame(self); frm.pack(fill="x", padx=8, pady=8)

        # Row 1
        self.symbol_var = tk.StringVar(value="GOLD#")
        self.tf_var     = tk.StringVar(value="M1")
        self.lot_var    = tk.DoubleVar(value=0.02)

        ttk.Label(frm, text="Symbol").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.symbol_var, width=12).grid(row=0, column=1, padx=6)

        ttk.Label(frm, text="TF").grid(row=0, column=2, sticky="w")
        ttk.Combobox(frm, textvariable=self.tf_var, values=["M1","M5","M15"], width=6, state="readonly").grid(row=0, column=3, padx=6)

        ttk.Label(frm, text="Lot").grid(row=0, column=4, sticky="w")
        ttk.Entry(frm, textvariable=self.lot_var, width=8).grid(row=0, column=5, padx=6)

        # Row 2
        self.tp_var = tk.IntVar(value=300)
        self.sl_var = tk.IntVar(value=150)
        self.spread_var = tk.IntVar(value=120)
        self.cooldown_var = tk.IntVar(value=30)
        self.maxpos_var = tk.IntVar(value=5)

        ttk.Label(frm, text="TP (pts)").grid(row=1, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.tp_var, width=8).grid(row=1, column=1, padx=6)

        ttk.Label(frm, text="SL (pts)").grid(row=1, column=2, sticky="w")
        ttk.Entry(frm, textvariable=self.sl_var, width=8).grid(row=1, column=3, padx=6)

        ttk.Label(frm, text="Max Spread").grid(row=1, column=4, sticky="w")
        ttk.Entry(frm, textvariable=self.spread_var, width=8).grid(row=1, column=5, padx=6)

        ttk.Label(frm, text="Cooldown (s)").grid(row=1, column=6, sticky="w")
        ttk.Entry(frm, textvariable=self.cooldown_var, width=8).grid(row=1, column=7, padx=6)

        ttk.Label(frm, text="Max Positions").grid(row=1, column=8, sticky="w")
        ttk.Entry(frm, textvariable=self.maxpos_var, width=8).grid(row=1, column=9, padx=6)

        # Row 3: confirmations + side
        self.rej_var = tk.BooleanVar(value=True)
        self.bos_var = tk.BooleanVar(value=True)
        self.confirm_mode_var = tk.StringVar(value="ANY")
        self.side_mode_var = tk.StringVar(value="BOTH")

        ttk.Checkbutton(frm, text="Use Rejection", variable=self.rej_var).grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(frm, text="Use mini BOS", variable=self.bos_var).grid(row=2, column=2, columnspan=2, sticky="w")

        ttk.Label(frm, text="Confirm Mode").grid(row=2, column=4, sticky="w")
        ttk.Combobox(frm, textvariable=self.confirm_mode_var, values=["ANY","BOTH"], width=6, state="readonly").grid(row=2, column=5, padx=6)

        ttk.Label(frm, text="Side Mode").grid(row=2, column=6, sticky="w")
        ttk.Combobox(frm, textvariable=self.side_mode_var, values=["BOTH","BUY_ONLY","SELL_ONLY"], width=10, state="readonly").grid(row=2, column=7, padx=6)

        # Buttons
        btnfrm = ttk.Frame(self); btnfrm.pack(fill="x", padx=8, pady=4)
        self.start_btn = ttk.Button(btnfrm, text="▶️ Start", command=self.on_start); self.start_btn.pack(side="left", padx=4)
        self.stop_btn  = ttk.Button(btnfrm, text="⏹ Stop",  command=self.on_stop, state="disabled"); self.stop_btn.pack(side="left", padx=4)
        ttk.Button(btnfrm, text="Test MT5", command=self.test_mt5).pack(side="left", padx=8)

        # Logs
        self.logbox = ScrolledText(self, height=24)
        self.logbox.pack(fill="both", expand=True, padx=8, pady=8)

        self.bot: Optional[ScalpBot] = None
        self.after(500, self._pulse)

    def test_mt5(self):
        try:
            if not mt5.initialize():
                self.log(f"init failed: {mt5.last_error()}"); return
            sym = self.symbol_var.get()
            if not mt5.symbol_select(sym, True):
                self.log(f"select symbol failed: {sym}")
            else:
                info = mt5.symbol_info(sym)
                self.log(f"OK: {sym} digits={info.digits} point={info.point} spread={info.spread}")
        finally:
            try: mt5.shutdown()
            except: pass

    def on_start(self):
        cfg = BotConfig(
            symbol=self.symbol_var.get().strip(),
            tf_name=self.tf_var.get(),
            lot_fixed=float(self.lot_var.get()),
            tp_mode="FIXED",                 # ปรับได้ถ้าต้องการ
            tp_points_fixed=int(self.tp_var.get()),
            sl_points_fixed=int(self.sl_var.get()),
            spread_max_points=int(self.spread_var.get()),
            cooldown_sec=int(self.cooldown_var.get()),
            max_open_pos=int(self.maxpos_var.get()),
            confirm_use_rejection=bool(self.rej_var.get()),
            confirm_use_bos=bool(self.bos_var.get()),
            confirm_mode=self.confirm_mode_var.get(),
            side_mode=self.side_mode_var.get(),
        )

        self.bot = ScalpBot(cfg, logger=self.log)
        self.bot.start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

    def on_stop(self):
        if self.bot:
            self.bot.stop()
            self.bot = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

    def log(self, text: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.logbox.insert("end", f"{ts} | {text}\n")
        self.logbox.see("end")

    def _pulse(self):
        # UI heartbeat
        self.after(500, self._pulse)


if __name__ == "__main__":
    app = App()
    app.mainloop()
