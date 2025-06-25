import MetaTrader5 as mt5
import pandas as pd
import time
import sys
import numpy as np
import os
from datetime import datetime

# 1. เชื่อมต่อ MT5
if not mt5.initialize():
    print("❌ initialize() ล้มเหลว, error code =", mt5.last_error())
    sys.exit()

symbol = "GOLD#"
lot = 0.01


def get_supported_filling_mode(symbol):
    info = mt5.symbol_info(symbol)
    if info is None:
        print(f"❌ ไม่พบข้อมูล symbol: {symbol}")
        return None

    for mode in [
        mt5.ORDER_FILLING_IOC,
        mt5.ORDER_FILLING_RETURN,
        mt5.ORDER_FILLING_FOK,
    ]:
        if info.filling_mode & mode:
            return mode

    print(f"❌ Symbol {symbol} ไม่รองรับ filling_mode ใดเลย")
    return None


def get_data(symbol, timeframe=mt5.TIMEFRAME_M15, n=100):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
    if rates is None or len(rates) == 0:
        print(f"❌ ไม่พบข้อมูลแท่งเทียนสำหรับ {symbol}")
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", errors="coerce")
    return df


def is_support(df, i):
    return (
        df["low"].iloc[i] < df["low"].iloc[i - 1]
        and df["low"].iloc[i] < df["low"].iloc[i + 1]
    )


def is_resistance(df, i):
    return (
        df["high"].iloc[i] > df["high"].iloc[i - 1]
        and df["high"].iloc[i] > df["high"].iloc[i + 1]
    )


def calculate_pivot_points(df):
    if len(df) < 1:
        return None, None
    high = df["high"].iloc[-1]
    low = df["low"].iloc[-1]
    close = df["close"].iloc[-1]
    pivot = (high + low + close) / 3
    r1 = 2 * pivot - low
    s1 = 2 * pivot - high
    return s1, r1


def calculate_fibonacci_levels(df, lookback=20):
    if len(df) < lookback:
        return {}
    high = df["high"].iloc[-lookback:].max()
    low = df["low"].iloc[-lookback:].min()
    diff = high - low
    return {
        "fib_0": low,
        "fib_23.6": high - 0.236 * diff,
        "fib_38.2": high - 0.382 * diff,
        "fib_50": high - 0.5 * diff,
        "fib_61.8": high - 0.618 * diff,
        "fib_100": high,
    }


def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ⬇️ เพิ่มในฟังก์ชัน get_data
def get_data(symbol, timeframe=mt5.TIMEFRAME_M15, n=100):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
    if rates is None or len(rates) == 0:
        print(f"❌ ไม่พบข้อมูลแท่งเทียนสำหรับ {symbol}")
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", errors="coerce")
    return df

# ✅ ข้อ 1: Volume spike filter
def is_volume_spike(df, threshold=1.5):
    avg_volume = df["real_volume"].rolling(window=20).mean()
    return df["real_volume"].iloc[-1] > threshold * avg_volume.iloc[-1]

# ✅ ข้อ 2: RSI Divergence (พื้นฐาน)
def detect_rsi_divergence(df):
    if len(df) < 5 or df["rsi"].isna().sum() > 0:
        return False
    return (
        df["low"].iloc[-2] < df["low"].iloc[-3]
        and df["rsi"].iloc[-2] > df["rsi"].iloc[-3]
    )

# ✅ ข้อ 3: ATR-based SL/TP
def calculate_atr(df, period=14):
    df["H-L"] = df["high"] - df["low"]
    df["H-PC"] = abs(df["high"] - df["close"].shift(1))
    df["L-PC"] = abs(df["low"] - df["close"].shift(1))
    tr = df[["H-L", "H-PC", "L-PC"]].max(axis=1)
    atr = tr.rolling(window=period).mean()
    return atr

# ✅ ข้อ 5: Logging
def log_trade(action, price, trend, rsi, support, resistance, fib, result):
    filename = "trade_log.csv"
    header = not os.path.exists(filename)
    with open(filename, "a") as f:
        if header:
            f.write("datetime,action,price,trend,rsi,support,resistance,fib,result\n")
        f.write(
            f"{datetime.now()},{action},{price},{trend},{rsi:.2f},{support:.2f},{resistance:.2f},{fib},{result.retcode if result else 'N/A'}\n"
        )

def find_support_resistance_advanced(df, ma_period=20):
    if len(df) < ma_period + 2:
        print("❌ ข้อมูลไม่พอสำหรับการวิเคราะห์")
        return None, None, None, {}, None

    # คำนวณ pivot และ swing high/low
    pivot_support, pivot_resistance = calculate_pivot_points(df)
    swing_supports = [
        df["low"].iloc[i] for i in range(1, len(df) - 1) if is_support(df, i)
    ]
    swing_resistances = [
        df["high"].iloc[i] for i in range(1, len(df) - 1) if is_resistance(df, i)
    ]

    support = (
        max(min(swing_supports), pivot_support) if swing_supports else pivot_support
    )
    resistance = (
        min(max(swing_resistances), pivot_resistance)
        if swing_resistances
        else pivot_resistance
    )

    # ค่าเฉลี่ยเคลื่อนที่
    df["ma_fast"] = df["close"].rolling(window=10).mean()
    df["ma_slow"] = df["close"].rolling(window=ma_period).mean()

    # คำนวณ RSI
    df["rsi"] = calculate_rsi(df)

    # ค่าล่าสุด
    last_close = df["close"].iloc[-1]
    ma_fast = df["ma_fast"].iloc[-1]
    ma_slow = df["ma_slow"].iloc[-1]
    ma_slope = df["ma_slow"].diff().iloc[-1]
    last_rsi = df["rsi"].iloc[-1]

    # ตัดสินใจเทรนด์ที่แม่นยำขึ้น
    if pd.isna(ma_fast) or pd.isna(ma_slow):
        trend = "unknown"
    elif ma_fast > ma_slow and ma_slope > 0 and last_close > ma_fast:
        trend = "uptrend"
    elif ma_fast < ma_slow and ma_slope < 0 and last_close < ma_fast:
        trend = "downtrend"
    else:
        trend = "sideways"

    # Fibonacci
    fib_levels = calculate_fibonacci_levels(df)

    return support, resistance, trend, fib_levels, last_rsi



def send_order(symbol, lot, order_type, sl_price, tp_price):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        print("❌ ไม่สามารถดึงข้อมูล tick ได้")
        return None

    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid

    for mode in [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]:
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": sl_price,
            "tp": tp_price,
            "deviation": 10,
            "magic": 123456,
            "comment": f"Auto Order (mode={mode})",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mode,
        }

        result = mt5.order_send(request)

        if result is None:
            print(f"❌ ส่งคำสั่งไม่สำเร็จ (mode={mode}) → result=None")
            continue

        print("📤 ส่งคำสั่ง:", result)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            print("✅ คำสั่งสำเร็จ!")
            return result  # ✅ คืนค่าจริง

        print(f"⚠️ คำสั่งล้มเหลว: retcode={result.retcode}, msg={result.comment}")

    print("❌ คำสั่งไม่สำเร็จในทุก mode")
    return None


def close_all_orders(symbol):
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        print("❌ ไม่สามารถดึงรายการ position ได้")
        return

    for pos in positions:
        opposite_order_type = (
            mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        )
        tick = mt5.symbol_info_tick(symbol)
        price = tick.bid if opposite_order_type == mt5.ORDER_TYPE_SELL else tick.ask

        result = send_order(symbol, pos.volume, opposite_order_type, 0, 0)
        print(f"📤 ปิด Order {pos.ticket}, ปริมาณ: {pos.volume}, ผลลัพธ์: {result}")


def open_buy_order(symbol, lot):
    tick = mt5.symbol_info_tick(symbol)
    point = mt5.symbol_info(symbol).point
    sl_price = tick.ask - 500 * point
    tp_price = tick.ask + 1500 * point
    return send_order(symbol, lot, mt5.ORDER_TYPE_BUY, sl_price, tp_price)


def open_sell_order(symbol, lot):
    tick = mt5.symbol_info_tick(symbol)
    point = mt5.symbol_info(symbol).point
    sl_price = tick.bid + 500 * point
    tp_price = tick.bid - 1500 * point
    return send_order(symbol, lot, mt5.ORDER_TYPE_SELL, sl_price, tp_price)


def close_positions(symbol, position_type, opposite_order_type):
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        print("❌ ไม่สามารถดึงรายการ position ได้")
        return
    
    for pos in positions:
        if pos.type == position_type:
            tick = mt5.symbol_info_tick(symbol)
            price = tick.bid if opposite_order_type == mt5.ORDER_TYPE_SELL else tick.ask
            
            result = send_order(symbol, pos.volume, opposite_order_type, 0, 0)

            if result is None:
                print("⚠️ การส่งคำสั่งล้มเหลว (result = None)")
                continue  # ข้ามไป position ถัดไป

            if result.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"✅ ปิดสถานะ {symbol} จำนวน {pos.volume} lot สำเร็จ")
            else:
                print(f"❌ ปิดสถานะไม่สำเร็จ: retcode={result.retcode}, msg={result.comment}")

def calculate_lot_size(risk_percentage=1, account_balance=None, atr=None, stop_loss_pips=None):
    if account_balance is None or atr is None or stop_loss_pips is None:
        return 0.01  # fallback

    # เงินที่พร้อมจะเสียในแต่ละ order
    risk_amount = account_balance * (risk_percentage / 100)
    
    # มูลค่าต่อ pip
    pip_value = 10  # สำหรับทองในบางโบรกเกอร์อาจไม่ใช่ 10 ต้องตรวจสอบที่ mt5.symbol_info(symbol)

    lot_size = risk_amount / (stop_loss_pips * pip_value)
    return max(round(lot_size, 2), 0.01)

def is_good_time_to_trade():
    now = datetime.now()
    if now.hour in [3, 4, 5]:  # ช่วงเวลาน้ำบาง (ตลาด New York ปิด)
        return False
    return True

def modify_order_trailing(symbol, distance=500):
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return

    for pos in positions:
        tick = mt5.symbol_info_tick(symbol)
        price = tick.ask if pos.type == mt5.POSITION_TYPE_BUY else tick.bid
        point = mt5.symbol_info(symbol).point

        if pos.type == mt5.POSITION_TYPE_BUY and (price - pos.price_open) > distance * point:
            new_sl = price - distance * point
        elif pos.type == mt5.POSITION_TYPE_SELL and (pos.price_open - price) > distance * point:
            new_sl = price + distance * point
        else:
            continue

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": pos.ticket,
            "sl": new_sl,
            "tp": pos.tp,
        }

        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"🔒 ปรับ SL ใหม่เป็น {new_sl}")

def should_stop_trading():
    account_info = mt5.account_info()
    if account_info is None:
        return False

    balance = account_info.balance
    equity = account_info.equity

    if equity < balance * 0.95:
        print("🛑 ขาดทุนเกิน 5% - หยุดเทรดวันนี้")
        return True
    return False

def main_loop():
    if not mt5.initialize() or not mt5.terminal_info():
        print("❌ ไม่สามารถเชื่อมต่อ MT5 ได้")
        return

    try:
        while True:
            df = get_data(symbol, timeframe=mt5.TIMEFRAME_H1, n=100)
            _, _, major_trend, _, _ = find_support_resistance_advanced(df)

            if df.empty:
                time.sleep(60 * 15)
                continue

            support, resistance, trend, fib_levels, rsi = (
                find_support_resistance_advanced(df)
            )
            if None in (support, resistance, trend, rsi):
                time.sleep(60 * 15)
                continue

            if major_trend != trend:
                print("⚡ เทรนด์ H1 ขัดกับ M15 - ไม่เข้าเทรด")
                time.sleep(60 * 15)
                continue

            modify_order_trailing(symbol)

            # 🔄 ตรวจสอบการเปลี่ยนเทรนด์
            # if trend != last_trend:
            #     print(f"🔁 Trend เปลี่ยนจาก {last_trend} → {trend}")

            #     if trend == "uptrend":
            #         close_positions(symbol, mt5.POSITION_TYPE_SELL, mt5.ORDER_TYPE_BUY)
            #     elif trend == "downtrend":
            #         close_positions(symbol, mt5.POSITION_TYPE_BUY, mt5.ORDER_TYPE_SELL)
            #     elif trend == 'sideways':
            #         close_positions(symbol, mt5.POSITION_TYPE_SELL, mt5.ORDER_TYPE_BUY)
            #         close_positions(symbol, mt5.POSITION_TYPE_BUY, mt5.ORDER_TYPE_SELL)

            #     last_trend = trend  # อัปเดตค่า trend ล่าสุด

            tick = mt5.symbol_info_tick(symbol)
            bid_price = tick.bid
            ask_price = tick.ask

            positions = mt5.positions_get(symbol=symbol)
            has_buy = any(p.type == mt5.POSITION_TYPE_BUY for p in positions) if positions else False
            has_sell = any(p.type == mt5.POSITION_TYPE_SELL for p in positions) if positions else False

            # ฟังก์ชันช่วยเทียบว่า "ใกล้" level ไหม
            def near(price, level, tolerance=0.0015):
                return abs(price - level) <= level * tolerance
            
            atr = calculate_atr(df).iloc[-1]

            # Check volume spike
            volume_ok = is_volume_spike(df)
            divergence = detect_rsi_divergence(df)

            print(f"📊 Bid: {bid_price:.2f}, Ask: {ask_price:.2f}, Trend: {trend}, S: {support:.2f}, R: {resistance:.2f}, RSI: {rsi:.2f}")
            print(f"📊 Vol: {volume_ok}, Divergence: {divergence}, atr: {atr} ")
            print(f"📊 has_buy: {has_buy}, has_sell: {has_sell},")
            print(f"🔢 Fib: {fib_levels}")  # พิมพ์ค่า Fibonacci levels
            print(f"🔢 Time to trade: {is_good_time_to_trade()}")
            print(f"🔢 Lot Size: {calculate_lot_size()}")

            if not is_good_time_to_trade():
                print("⏳ เวลานี้ไม่เหมาะกับการเทรด")
                time.sleep(60 * 15)
                continue

            # เงื่อนไขกลยุทธ์
            if trend == "uptrend" and rsi < 70:
                if (
                    near(ask_price, support) or 
                    near(ask_price, fib_levels.get('fib_38.2', 0))
                ):
                    sl = ask_price - 1.5 * atr
                    tp = ask_price + 3 * atr
                    result = send_order(symbol, calculate_lot_size(), mt5.ORDER_TYPE_BUY, sl, tp)

            elif trend == "downtrend" and rsi > 30:
                if (
                    near(bid_price, resistance) or 
                    near(bid_price, fib_levels.get('fib_61.8', 999999))
                ):
                    sl = bid_price + 1.5 * atr
                    tp = bid_price - 3 * atr
                    result = send_order(symbol, calculate_lot_size(), mt5.ORDER_TYPE_SELL, sl, tp)
            else:
                print("📉 Sideway หรือ RSI/Fib ไม่เข้าเงื่อนไข - ไม่ทำรายการ")

            print("⏳ รอรอบถัดไป...\n")
            time.sleep(60 * 15)

    except KeyboardInterrupt:
        print("\n🛑 หยุดด้วย KeyboardInterrupt")
    finally:
        mt5.shutdown()
        print("🔌 ปิดการเชื่อมต่อ MT5")


if __name__ == "__main__":
    main_loop()
