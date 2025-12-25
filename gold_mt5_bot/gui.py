from __future__ import annotations

import threading
import tkinter as tk
from tkinter import scrolledtext, messagebox
from pathlib import Path

import yaml

from gold_mt5_bot.bot.runner import runner


class BotGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Gold MT5 Bot")
        self.geometry("720x520")

        self.config_var = tk.StringVar(value=str(Path(__file__).resolve().parent / "config.yaml"))

        self._build_layout()
        self._update_status()
        self._poll_logs()

    def _build_layout(self) -> None:
        top = tk.Frame(self)
        top.pack(fill=tk.X, padx=10, pady=10)

        tk.Label(top, text="Config path:").pack(side=tk.LEFT)
        tk.Entry(top, textvariable=self.config_var, width=60).pack(side=tk.LEFT, padx=5)
        tk.Button(top, text="Start", command=self._start_bot, bg="#4caf50", fg="white").pack(side=tk.LEFT, padx=5)
        tk.Button(top, text="Stop", command=self._stop_bot, bg="#f44336", fg="white").pack(side=tk.LEFT, padx=5)

        self.status_label = tk.Label(self, text="Status: stopped")
        self.status_label.pack(anchor="w", padx=10)

        # Config quick edits
        cfg_frame = tk.LabelFrame(self, text="Config editor (quick changes)")
        cfg_frame.pack(fill=tk.X, padx=10, pady=5)
        self.symbol_var = tk.StringVar()
        self.spread_var = tk.StringVar()
        self.dry_run_var = tk.BooleanVar(value=True)

        tk.Label(cfg_frame, text="Symbol").grid(row=0, column=0, sticky="w")
        tk.Entry(cfg_frame, textvariable=self.symbol_var, width=12).grid(row=0, column=1, padx=5)
        tk.Label(cfg_frame, text="Spread limit").grid(row=0, column=2, sticky="w")
        tk.Entry(cfg_frame, textvariable=self.spread_var, width=8).grid(row=0, column=3, padx=5)
        tk.Checkbutton(cfg_frame, text="Dry run", variable=self.dry_run_var).grid(row=0, column=4, padx=5)
        tk.Button(cfg_frame, text="Save config", command=self._save_config).grid(row=0, column=5, padx=5)

        self.log_box = scrolledtext.ScrolledText(self, height=20, bg="#111", fg="#0f0", insertbackground="white")
        self.log_box.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.log_box.configure(state=tk.DISABLED)

    def _start_bot(self) -> None:
        cfg = self.config_var.get().strip()
        if not cfg:
            messagebox.showerror("Error", "Config path is empty")
            return
        runner.start(config_path=cfg)
        self._update_status()

    def _stop_bot(self) -> None:
        runner.stop()
        self._update_status()

    def _save_config(self) -> None:
        cfg_path = Path(self.config_var.get().strip())
        if not cfg_path.is_absolute():
            cfg_path = Path(__file__).resolve().parent / cfg_path
        if not cfg_path.exists():
            messagebox.showerror("Error", f"Config not found: {cfg_path}")
            return
        try:
            with cfg_path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
            if "mt5" in raw and self.symbol_var.get():
                raw["mt5"]["symbol"] = self.symbol_var.get().strip()
            if "strategy" in raw and self.spread_var.get():
                try:
                    raw["strategy"]["spread_limit_points"] = float(self.spread_var.get())
                except ValueError:
                    messagebox.showerror("Error", "Spread limit must be a number")
                    return
            if "execution" in raw:
                raw["execution"]["dry_run"] = bool(self.dry_run_var.get())
            with cfg_path.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(raw, fh, sort_keys=False)
            messagebox.showinfo("Saved", f"Config saved to {cfg_path}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Error", f"Failed to save config: {exc}")

    def _update_status(self) -> None:
        status = runner.status()
        text = f"Status: running={status['running']} | config={status['config']} | last_error={status['last_error']}"
        self.status_label.config(text=text)
        self.after(1000, self._update_status)

    def _poll_logs(self) -> None:
        log_path = Path("logs/bot.log")
        content = ""
        if log_path.exists():
            try:
                with log_path.open("r", encoding="utf-8", errors="ignore") as fh:
                    lines = fh.readlines()
                content = "".join(lines[-500:])
            except Exception:
                content = ""
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.delete("1.0", tk.END)
        self.log_box.insert(tk.END, content)
        self.log_box.configure(state=tk.DISABLED)
        self.after(2000, self._poll_logs)


def main() -> None:
    app = BotGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
