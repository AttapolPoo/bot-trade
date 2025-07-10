import tkinter as tk
from tkinter import ttk, messagebox
import threading
import MetaTrader5 as mt5
import gold_bot
import time  # เพิ่มบรรทัดนี้ที่ import ด้านบน


class TradingApp:
    def __init__(self, master):
        self.master = master
        master.title("Gold Trading Bot")

        # Input fields
        tk.Label(master, text="Symbol:").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        tk.Label(master, text="Lot Size:").grid(row=1, column=0, padx=5, pady=5, sticky="w")
        tk.Label(master, text="Timeframe:").grid(row=2, column=0, padx=5, pady=5, sticky="w")

        self.symbol_entry = tk.Entry(master)
        self.symbol_entry.insert(0, "GOLD#")
        self.symbol_entry.grid(row=0, column=1, padx=5, pady=5)

        self.lot_entry = tk.Entry(master)
        self.lot_entry.insert(0, "0.01")
        self.lot_entry.grid(row=1, column=1, padx=5, pady=5)

        self.timeframe_combobox = ttk.Combobox(master, values=['M1', 'M5', 'M15', 'H1', 'H4', 'D1'], state="readonly")
        self.timeframe_combobox.current(2)  # Default M15
        self.timeframe_combobox.grid(row=2, column=1, padx=5, pady=5)

        # Control buttons
        self.start_button = tk.Button(master, text="Start", command=self.start_bot)
        self.stop_button = tk.Button(master, text="Stop", command=self.stop_bot, state='disabled')
        self.start_button.grid(row=3, column=0, padx=5, pady=5)
        self.stop_button.grid(row=3, column=1, padx=5, pady=5)

        # Log window
        self.log_text = tk.Text(master, height=20, width=60)
        self.log_text.grid(row=4, column=0, columnspan=2, padx=5, pady=5)

        self.running = False
        self.bot_thread = None

        self.TIMEFRAME_MAP = {
            'M1': mt5.TIMEFRAME_M1,
            'M5': mt5.TIMEFRAME_M5,
            'M15': mt5.TIMEFRAME_M15,
            'H1': mt5.TIMEFRAME_H1,
            'H4': mt5.TIMEFRAME_H4,
            'D1': mt5.TIMEFRAME_D1,
        }

        # Handle closing
        master.protocol("WM_DELETE_WINDOW", self.on_closing)

    # เพิ่ม Label แสดงนาฬิกา
        self.clock_label = tk.Label(master, text="", font=("Helvetica", 12))
        self.clock_label.grid(row=5, column=0, columnspan=2, pady=(0,10))
        
        # เริ่มอัพเดตนาฬิกา
        self.update_clock()

    def update_clock(self):
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")
        self.clock_label.config(text="Current Time: " + current_time)
        self.master.after(1000, self.update_clock)  # เรียกตัวเองทุก 1000ms = 1 วินาที

    def start_bot(self):
        if self.running:
            messagebox.showinfo("Info", "Bot is already running!")
            return

        self.symbol = self.symbol_entry.get()
        try:
            self.lot = float(self.lot_entry.get())
        except ValueError:
            messagebox.showerror("Input Error", "Please enter a valid lot size.")
            return

        selected_tf = self.timeframe_combobox.get()
        self.timeframe = self.TIMEFRAME_MAP.get(selected_tf, mt5.TIMEFRAME_M15)

        self.running = True
        self.start_button.config(state='disabled')
        self.stop_button.config(state='normal')

        self.bot_thread = threading.Thread(target=self.run_bot, daemon=True)
        self.bot_thread.start()
        self.update_log("✅ Bot started.")

    def run_bot(self):
        try:
            gold_bot.main_loop(
                symbol=self.symbol,
                lot=self.lot,
                timeframe=self.timeframe,
                stop_callback=self.stop_check,
                log_callback=self.update_log
            )
        except Exception as e:
            self.update_log(f"❌ Bot crashed: {str(e)}")
        finally:
            self.running = False
            self.start_button.config(state='normal')
            self.stop_button.config(state='disabled')

    def stop_check(self):
        return not self.running

    def stop_bot(self):
        if self.running:
            self.running = False
            self.update_log("🛑 Stop command sent.")
            self.start_button.config(state='normal')
            self.stop_button.config(state='disabled')
        else:
            messagebox.showinfo("Info", "Bot is not running.")

    def update_log(self, text):
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)

    def on_closing(self):
        if self.running:
            if messagebox.askokcancel("Quit", "Bot is still running. Do you want to quit?"):
                self.running = False
                self.master.destroy()
        else:
            self.master.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = TradingApp(root)
    root.mainloop()
