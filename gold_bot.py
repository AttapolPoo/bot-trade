import sys
import MetaTrader5 as mt5
import pandas as pd
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton, QLabel,
    QLineEdit, QTableWidget, QTableWidgetItem, QHBoxLayout, QSpinBox
)
from PyQt5.QtCore import QTimer, QDateTime
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
import matplotlib.pyplot as plt
from datetime import datetime

from PyQt5.QtWidgets import QTableWidgetItem
import matplotlib.lines as mlines

symbol = "GOLD#"
timeframe = mt5.TIMEFRAME_M1

if not mt5.initialize():
    print("❌ initialize() ล้มเหลว, error code =", mt5.last_error())
    exit()

def get_data(symbol, timeframe=mt5.TIMEFRAME_M1, n=100):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df

def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

# ฟังก์ชันหาแนวรับแนวต้านง่าย ๆ (swing low / swing high)
def find_support_resistance(df):
    supports = [df["low"].iloc[i] for i in range(1, len(df)-1) if df["low"].iloc[i] < df["low"].iloc[i-1] and df["low"].iloc[i] < df["low"].iloc[i+1]]
    resistances = [df["high"].iloc[i] for i in range(1, len(df)-1) if df["high"].iloc[i] > df["high"].iloc[i-1] and df["high"].iloc[i] > df["high"].iloc[i+1]]
    support = min(supports) if supports else df["low"].min()
    resistance = max(resistances) if resistances else df["high"].max()
    return support, resistance

def find_best_entry_point(df, entry_tolerance=0.005):
    if df.empty or len(df) < 20:
        return "No Data", None, None

    df["rsi"] = calculate_rsi(df)
    ma_fast = df["close"].rolling(window=10).mean()
    ma_slow = df["close"].rolling(window=20).mean()
    last_close = df["close"].iloc[-1]
    last_rsi = df["rsi"].iloc[-1]

    support, resistance = find_support_resistance(df)

    if ma_fast.iloc[-1] > ma_slow.iloc[-1]:
        trend = "Uptrend"
    elif ma_fast.iloc[-1] < ma_slow.iloc[-1]:
        trend = "Downtrend"
    else:
        trend = "Sideways"

    if trend == "Uptrend":
        if abs(last_close - support) / support <= entry_tolerance and last_rsi < 70:
            return f"Buy @ {last_close:.2f}", df["time"].iloc[-1], "buy"
        else:
            return "Wait for Buy Zone", None, None
    elif trend == "Downtrend":
        if abs(last_close - resistance) / resistance <= entry_tolerance and last_rsi > 30:
            return f"Sell @ {last_close:.2f}", df["time"].iloc[-1], "sell"
        else:
            return "Wait for Sell Zone", None, None
    else:
        return "No Clear Entry", None, None
    
def calculate_ema(series, period=14):
    return series.ewm(span=period, adjust=False).mean()
    
class RealTimeTradingApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("📊 Real-Time Trend Dashboard with Chart")
        self.setGeometry(100, 100, 800, 700)

        self.symbol_input = QLineEdit(self)
        self.symbol_input.setText("GOLD#")

        self.interval_input = QSpinBox(self)
        self.interval_input.setValue(60)
        self.interval_input.setSuffix(" sec")

        self.refresh_button = QPushButton("🔄 Refresh Now", self)
        self.refresh_button.clicked.connect(self.refresh_data)

        self.last_update_label = QLabel("", self)

        self.entry_tolerance = 0.003  # กำหนด tolerance = 0.3% (0.003)

        self.table = QTableWidget(self)
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Timeframe", "Trend", "Reversal", "RSI", "Entry Signal", "Best Entry"])

        # Layout
        layout = QVBoxLayout()
        input_layout = QHBoxLayout()
        input_layout.addWidget(QLabel("Symbol:"))
        input_layout.addWidget(self.symbol_input)
        input_layout.addWidget(QLabel("Interval:"))
        input_layout.addWidget(self.interval_input)
        input_layout.addWidget(self.refresh_button)

        layout.addLayout(input_layout)
        layout.addWidget(self.last_update_label)
        layout.addWidget(self.table)

        self.setLayout(layout)

        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh_data)
        self.timer.start(self.interval_input.value() * 1000)

        self.interval_input.valueChanged.connect(self.update_timer_interval)

        self.refresh_data()

    def update_timer_interval(self):
        self.timer.setInterval(self.interval_input.value() * 1000)

    def refresh_data(self):
        current_symbol = self.symbol_input.text().strip() or "GOLD#"
        self.update_table(current_symbol)

        self.last_update_label.setText(f"⏰ Last Update: {QDateTime.currentDateTime().toString('yyyy-MM-dd HH:mm:ss')}")

    def update_table(self, current_symbol):
        self.signals = []  # เก็บสัญญาณทั้งหมด
        timeframes = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
        }

        report = []
        for tf_name, tf_value in timeframes.items():
            df = get_data(current_symbol, timeframe=tf_value, n=100)
            if df.empty:
                report.append([tf_name, 'No Data', 'N/A', 'N/A', 'No Signal', 'No Data'])
                continue

            df["rsi"] = calculate_rsi(df)
            ema_fast = calculate_ema(df["close"], period=10)
            ema_slow = calculate_ema(df["close"], period=20)
            last_close = df["close"].iloc[-1]

            if ema_fast.iloc[-1] > ema_slow.iloc[-1]:
                trend = "Uptrend"
            elif ema_fast.iloc[-1] < ema_slow.iloc[-1]:
                trend = "Downtrend"
            else:
                trend = "Sideways"

            divergence = 'Yes' if df["rsi"].iloc[-2] > df["rsi"].iloc[-3] else 'No'
            rsi = f"{df['rsi'].iloc[-1]:.2f}"

            support, resistance = find_support_resistance(df)
            entry_signal = "No Signal"
            tol = self.entry_tolerance
            if trend == "Uptrend" and abs(last_close - support) / support < tol:
                entry_signal = "Buy Near Support"
            elif trend == "Downtrend" and abs(last_close - resistance) / resistance < tol:
                entry_signal = "Sell Near Resistance"

            best_entry, signal_time, signal_type = find_best_entry_point(df)

            if signal_time:
                self.signals.append({
                    "time": signal_time,
                    "price": last_close,
                    "type": signal_type,
                    "timeframe": tf_name
                })

            report.append([tf_name, trend, divergence, rsi, entry_signal, best_entry])

        self.table.setRowCount(len(report))
        for row, data in enumerate(report):
            for col, value in enumerate(data):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = RealTimeTradingApp()
    window.show()
    sys.exit(app.exec_())
