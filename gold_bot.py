import MetaTrader5 as mt5
import pandas as pd
import time
import sys
import numpy as np
import os
from datetime import datetime, timedelta
from typing import Optional, Tuple

"""
Scalp Bot (M1) — S/R & Order Block only
- TF เดียว: M1
- เข้าเมื่อราคามาใกล้แนวรับ/แนวต้าน หรือแตะ Order Block (M1)
- TP สั้น 300 จุด และ SL 150 จุด (หน่วย points)
- ตัวกรอง: สเปรด, คูลดาวน์, จำกัดจำนวนโพซิชัน, ปิดโพซิชันที่ค้างนาน
- FIX: ลอง filling mode หลายแบบ (FOK→IOC→RETURN→ไม่ส่ง field)
- SAFE: ปัดราคา/ลอตตาม digits/volume_step, deviation ตามสเปรด
"""

# ======================
# CONFIG
# ======================
SYMBOL = "GOLD#"                  # *ปรับตามโบรกของคุณ*
TF     = mt5.TIMEFRAME_M1
HISTORY_BARS = 1200

# ขนาดออเดอร์
LOT_FIXED = 0.02                  # lot ตายตัวสำหรับสแกัลป์

# เป้ากำไร/ขาดทุนสั้น ๆ (หน่วย "points")
TP_MODE          = "FIXED"        # "FIXED" | "RANGE_RANDOM" | "ADAPTIVE_TO_ZONE"
TP_POINTS_FIXED  = 300            # **ตามที่ขอ**
SL_POINTS_FIXED  = 150            # **ตามที่ขอ**
TP_POINTS_MIN    = 100            # ใช้เมื่อ RANGE_RANDOM / ADAPTIVE_TO_ZONE
TP_POINTS_MAX    = 300

# ใกล้โซนแค่ไหนจึงเข้า (หน่วย "points")
NEAR_POINTS      = 60

# สเปรดสูงสุดที่ยอมรับ (หน่วย "points")
SPREAD_MAX_POINTS = 120

# จำกัดระบบ
COOLDOWN_SEC      = 30            # คูลดาวน์ต่อฝั่ง
MAX_OPEN_POS      = 5             # จำกัดจำนวนโพซิชันรวม
MAX_HOLD_SEC      = 900           # ปิดโพซิชันถ้าค้างเกินเวลานี้ (0=ปิดฟีเจอร์)

# Pivot/Order Block
SR_LOOKBACK        = 80           # บาร์หลังสุดที่ใช้หา swing S/R
OB_LOOKBACK_BARS   = 200          # ช่วงค้นหา OB
BOS_LOOKBACK       = 8            # เช็ค BOS ย้อนหลัง
OB_PAD_POINTS      = 20           # ขยายโซน OB ตอนเช็คสัมผัส

# ลำดับ filling mode ที่จะลอง
FILLING_TRY_ORDER = [
    mt5.ORDER_FILLING_FOK,
    mt5.ORDER_FILLING_IOC,
    mt5.ORDER_FILLING_RETURN,
    None,  # ไม่ส่ง type_filling (ให้ server ตัดสิน)
]

# ======================
# INIT
# ======================
if not mt5.initialize():
    print("❌ initialize() ล้มเหลว:", mt5.last_error())
    sys.exit()

if not mt5.symbol_select(SYMBOL, True):
    print(f"❌ เลือก symbol {SYMBOL} ไม่ได้")
    mt5.shutdown()
    sys.exit()

# ======================
# HELPERS (Levels/OB)
# ======================
def get_data(symbol: str, timeframe=TF, n=HISTORY_BARS) -> pd.DataFrame:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", errors="coerce")
    return df

def is_support(df: pd.DataFrame, i: int) -> bool:
    return (df["low"].iloc[i] < df["low"].iloc[i-1]) and (df["low"].iloc[i] < df["low"].iloc[i+1])

def is_resistance(df: pd.DataFrame, i: int) -> bool:
    return (df["high"].iloc[i] > df["high"].iloc[i-1]) and (df["high"].iloc[i] > df["high"].iloc[i+1])

def find_sr_levels(df: pd.DataFrame, lookback: int = SR_LOOKBACK) -> Tuple[Optional[float], Optional[float]]:
    if len(df) < lookback + 2:
        return None, None
    s_levels = [df["low"].iloc[i]  for i in range(len(df)-lookback, len(df)-1)
                if 0 < i < len(df)-1 and is_support(df, i)]
    r_levels = [df["high"].iloc[i] for i in range(len(df)-lookback, len(df)-1)
                if 0 < i < len(df)-1 and is_resistance(df, i)]
    s = max(s_levels) if s_levels else None
    r = min(r_levels) if r_levels else None
    return s, r

def find_order_blocks_m1(df: pd.DataFrame,
                         lookback: int = OB_LOOKBACK_BARS,
                         bos_back: int = BOS_LOOKBACK) -> Tuple[Optional[Tuple[float,float]], Optional[Tuple[float,float]]]:
    """หา Bull/Bear OB แบบง่าย: มี BOS ขึ้น/ลง แล้วใช้แท่ง opposite สุดท้ายก่อน BOS เป็นโซน"""
    if len(df) < max(lookback, bos_back + 3):
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
            while j >= 1 and not (t["close"].iloc[j] < t["open"].iloc[j]):  # หาแท่งแดงล่าสุดก่อน BOS
                j -= 1
            if j >= 1:
                lo = float(min(t["open"].iloc[j], t["close"].iloc[j], t["low"].iloc[j]))
                hi = float(max(t["open"].iloc[j], t["close"].iloc[j]))
                bull_ob = (lo, hi)
        # BOS ลง
        prev_low = win["low"].min()
        if t["close"].iloc[i] < prev_low:
            j = i-1
            while j >= 1 and not (t["close"].iloc[j] > t["open"].iloc[j]):  # หาแท่งเขียวล่าสุดก่อน BOS
                j -= 1
            if j >= 1:
                lo = float(min(t["open"].iloc[j], t["close"].iloc[j]))
                hi = float(max(t["open"].iloc[j], t["close"].iloc[j], t["high"].iloc[j]))
                bear_ob = (lo, hi)
    return bull_ob, bear_ob

def is_near_level(price: float, level: Optional[float], tol_points: float, point: float) -> bool:
    if level is None or point <= 0:
        return False
    return abs(price - level) <= tol_points * point

def touch_zone_points(price: float, zone: Optional[Tuple[float,float]], pad_points: float, point: float) -> bool:
    if zone is None or point <= 0:
        return False
    lo, hi = zone
    if lo is None or hi is None:
        return False
    lo -= pad_points * point
    hi += pad_points * point
    return lo <= price <= hi

def spread_ok(symbol: str, max_points: int) -> bool:
    info = mt5.symbol_info(symbol)
    if not info:
        return True
    return (info.spread or 0) <= max_points

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

# ======================
# HELPERS (Trading utils)
# ======================
def normalize_volume(symbol: str, vol: float) -> float:
    info = mt5.symbol_info(symbol)
    if not info:
        return float(vol)
    step = info.volume_step or 0.01
    vmin = info.volume_min  or step
    vmax = info.volume_max  or vol
    vol_rounded = round(vol / step) * step
    return float(max(vmin, min(vmax, vol_rounded)))

def round_price(symbol: str, price: float) -> float:
    info = mt5.symbol_info(symbol)
    if not info:
        return float(price)
    digits = info.digits or 2
    return float(round(price, digits))

def filling_name(m):
    return {mt5.ORDER_FILLING_FOK: "FOK",
            mt5.ORDER_FILLING_IOC: "IOC",
            mt5.ORDER_FILLING_RETURN: "RETURN",
            None: "DEFAULT"}.get(m, str(m))

# --- TP chooser (ยังคงโครงสร้างเดิม แต่ตั้ง FIXED=300) ---
def choose_tp_points(info,
                     price_now: float,
                     side: str,
                     s: Optional[float],
                     r: Optional[float],
                     bull_ob: Optional[Tuple[float,float]],
                     bear_ob: Optional[Tuple[float,float]]) -> int:
    """คืนค่า TP distance (points) ตามโหมดที่เลือก + เคารพ stops_level"""
    stops_level_pts = info.trade_stops_level or 0
    if TP_MODE == "FIXED":
        tp_pts = TP_POINTS_FIXED
    elif TP_MODE == "RANGE_RANDOM":
        tp_pts = int(np.random.randint(TP_POINTS_MIN, TP_POINTS_MAX+1))
    else:  # ADAPTIVE_TO_ZONE
        point = info.point or 0.01
        if side == "buy":
            targets = []
            if r: targets.append(abs(r - price_now) / point)
            if bear_ob: targets.append(abs(bear_ob[0] - price_now) / point)
            tp_pts = int(clamp(min(targets) if targets else TP_POINTS_FIXED, TP_POINTS_MIN, TP_POINTS_MAX))
        else:
            targets = []
            if s: targets.append(abs(price_now - s) / point)
            if bull_ob: targets.append(abs(price_now - bull_ob[1]) / point)
            tp_pts = int(clamp(min(targets) if targets else TP_POINTS_FIXED, TP_POINTS_MIN, TP_POINTS_MAX))
    return max(tp_pts, stops_level_pts + 1)

# --- ส่งคำสั่ง: ลองหลาย filling mode + ใส่ SL/TP ตามจุด ---
def _order_send_try_fillings(req_base: dict, symbol: str):
    last_res = None
    for mode in FILLING_TRY_ORDER:
        req = req_base.copy()
        if mode is None:
            req.pop("type_filling", None)
        else:
            req["type_filling"] = mode
        res = mt5.order_send(req)
        print(f"📤 ส่งด้วย filling={filling_name(mode)} → {res}")
        last_res = res
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            return res
    return last_res

def send_market_order(symbol: str, lot: float, side: str, sl_points: int, tp_points: int):
    """
    ส่ง Market Order พร้อม SL/TP แบบ fixed points (เคารพ stops_level + ปัดราคา/ลอต)
    """
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    if not tick or not info:
        print("❌ ไม่มี tick หรือ symbol_info")
        return None

    print(f"ℹ️ SYMBOL filling_mode(raw)={getattr(info,'filling_mode', None)} "
          f"(spread={info.spread} pts, point={info.point}, digits={info.digits})")

    point = info.point or 0.01
    price = tick.ask if side == "buy" else tick.bid
    price = round_price(symbol, price)

    min_dist = (info.trade_stops_level or 0) * point

    if side == "buy":
        tp_price = price + max(tp_points * point, min_dist + point)
        sl_price = price - max(sl_points * point, min_dist + point)
        order_type = mt5.ORDER_TYPE_BUY
    else:
        tp_price = price - max(tp_points * point, min_dist + point)
        sl_price = price + max(sl_points * point, min_dist + point)
        order_type = mt5.ORDER_TYPE_SELL

    tp_price = round_price(symbol, tp_price)
    sl_price = round_price(symbol, sl_price)

    lot = normalize_volume(symbol, lot)
    deviation = max(10, int((info.spread or 0) * 2))

    req_base = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot),
        "type": order_type,
        "price": price,
        "sl": sl_price,
        "tp": tp_price,
        "deviation": deviation,
        "magic": 246810,
        "comment": f"Scalp-M1 SL{sl_points} TP{tp_points}",
        "type_time": mt5.ORDER_TIME_GTC,
    }
    return _order_send_try_fillings(req_base, symbol)

def close_stale_positions(symbol: str, max_age_sec: int):
    if max_age_sec <= 0:
        return
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
            req_base = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": float(p.volume),
                "type": close_type,
                "price": close_price,
                "deviation": deviation,
                "magic": 246810,
                "comment": "Stale close",
                "type_time": mt5.ORDER_TIME_GTC,
            }
            res = _order_send_try_fillings(req_base, symbol)
            print(f"⏰ ปิดโพซิชันค้างเกิน {max_age_sec}s → ticket={p.ticket}, ret={getattr(res, 'retcode', None)}")

# ======================
# MAIN LOOP
# ======================
def main_loop():
    last_trade_time = {"buy": datetime.min, "sell": datetime.min}

    try:
        while True:
            df = get_data(SYMBOL, TF, HISTORY_BARS)
            if df.empty or len(df) < SR_LOOKBACK + 10:
                print("⏳ รอข้อมูล…")
                time.sleep(1.0)
                continue

            info = mt5.symbol_info(SYMBOL)
            tick = mt5.symbol_info_tick(SYMBOL)
            if not info or not tick:
                time.sleep(1.0)
                continue

            point = info.point or 0.01
            bid, ask = tick.bid, tick.ask

            # ตัวกรองสเปรด
            if not spread_ok(SYMBOL, SPREAD_MAX_POINTS):
                print("⛔ สเปรดกว้างเกินกำหนด")
                time.sleep(1.0)
                continue

            # หาแนวรับ/แนวต้าน & OB (M1)
            s, r = find_sr_levels(df, SR_LOOKBACK)
            bull_ob, bear_ob = find_order_blocks_m1(df, OB_LOOKBACK_BARS, BOS_LOOKBACK)

            # เช็คใกล้โซน
            near_s = is_near_level(ask, s, NEAR_POINTS, point)
            near_r = is_near_level(bid, r, NEAR_POINTS, point)
            in_bull_ob = touch_zone_points(ask, bull_ob, OB_PAD_POINTS, point)
            in_bear_ob = touch_zone_points(bid, bear_ob, OB_PAD_POINTS, point)

            # จำกัดจำนวนโพซิชัน + คูลดาวน์
            poses = mt5.positions_get(symbol=SYMBOL) or []
            now = datetime.now()
            can_buy  = (now - last_trade_time["buy"]).total_seconds()  >= COOLDOWN_SEC
            can_sell = (now - last_trade_time["sell"]).total_seconds() >= COOLDOWN_SEC
            have_room = (len(poses) < MAX_OPEN_POS)

            # ปิดโพซิชันค้างนาน (ถ้าตั้งไว้)
            close_stale_positions(SYMBOL, MAX_HOLD_SEC)

            # ตัดสินใจ TP/SL (points)
            def pick_tp_points(side: str) -> int:
                return choose_tp_points(info, ask if side=="buy" else bid, side, s, r, bull_ob, bear_ob)
            def pick_sl_points() -> int:
                return SL_POINTS_FIXED

            # ===== เข้าซื้อ: ใกล้แนวรับ หรือ แตะ Bullish OB =====
            if have_room and can_buy and (near_s or in_bull_ob):
                tp_pts = pick_tp_points("buy")
                sl_pts = pick_sl_points()
                res = send_market_order(SYMBOL, LOT_FIXED, "buy", sl_pts, tp_pts)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    last_trade_time["buy"] = now

            # ===== เข้าขาย: ใกล้แนวต้าน หรือ แตะ Bearish OB =====
            if have_room and can_sell and (near_r or in_bear_ob):
                tp_pts = pick_tp_points("sell")
                sl_pts = pick_sl_points()
                res = send_market_order(SYMBOL, LOT_FIXED, "sell", sl_pts, tp_pts)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    last_trade_time["sell"] = now

            # แสดงสถานะสั้น ๆ
            print(f"[{now.strftime('%H:%M:%S')}] Bid:{bid:.2f} Ask:{ask:.2f} "
                  f"S:{'%.2f'%s if s else 'None'} R:{'%.2f'%r if r else 'None'}  "
                  f"nearS:{near_s} nearR:{near_r}  bullOB:{bull_ob} bearOB:{bear_ob}  open:{len(poses)}")

            time.sleep(1.0)

    except KeyboardInterrupt:
        print("\n🛑 หยุดด้วย KeyboardInterrupt")
    finally:
        mt5.shutdown()
        print("🔌 ปิดการเชื่อมต่อ MT5")

if __name__ == "__main__":
    main_loop()
