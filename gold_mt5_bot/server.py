from __future__ import annotations

import csv
from pathlib import Path
from typing import List

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from gold_mt5_bot.bot.runner import runner

app = FastAPI(title="Gold MT5 Bot Dashboard")


def read_tail(path: Path, lines: int = 200) -> str:
    if not path.exists():
        return ""
    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        content = fh.readlines()
    return "".join(content[-lines:])


def read_report(journal_path: Path) -> dict:
    if not journal_path.exists():
        return {"trades": 0, "wins": 0, "losses": 0, "avg_r": 0.0}
    wins = 0
    losses = 0
    total_r = 0.0
    trades = 0
    with journal_path.open("r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            trades += 1
            try:
                result = row.get("result", "")
                if result and result != "failed":
                    # derive R from TP/SL if available (simple placeholder)
                    total_r += 1
                    wins += 1
                else:
                    losses += 1
                    total_r -= 1
            except Exception:
                continue
    avg_r = total_r / trades if trades else 0.0
    return {"trades": trades, "wins": wins, "losses": losses, "avg_r": avg_r}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    status = runner.status()
    return f"""
    <html>
    <head>
      <title>Gold MT5 Bot</title>
      <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        pre {{ background: #111; color: #0f0; padding: 10px; height: 300px; overflow:auto; }}
        .card {{ border: 1px solid #ccc; padding: 10px; margin-bottom: 10px; }}
      </style>
    </head>
    <body>
      <h2>Gold MT5 Bot Dashboard</h2>
      <div class="card">
        <h3>Status</h3>
        <p>Running: {status["running"]}</p>
        <p>Config: {status["config"]}</p>
        <p>Last error: {status["last_error"]}</p>
        <form method="post" action="/start">
          <label>Config path: <input type="text" name="config_path" value="{status["config"]}"/></label>
          <button type="submit">Start</button>
        </form>
        <form method="post" action="/stop">
          <button type="submit">Stop</button>
        </form>
      </div>
      <div class="card">
        <h3>Logs</h3>
        <pre id="logs">{read_tail(Path("logs/bot.log"))}</pre>
      </div>
      <div class="card">
        <h3>Report</h3>
        <pre>{read_report(Path("trade_log.csv"))}</pre>
      </div>
    </body>
    </html>
    """


@app.post("/start")
def start(config_path: str = Form("config.yaml")) -> JSONResponse:
    runner.start(config_path=config_path)
    return JSONResponse({"status": "started", "config": config_path})


@app.post("/stop")
def stop() -> JSONResponse:
    runner.stop()
    return JSONResponse({"status": "stopped"})


@app.get("/status")
def status() -> JSONResponse:
    return JSONResponse(runner.status())


@app.get("/logs", response_class=PlainTextResponse)
def logs() -> str:
    return read_tail(Path("logs/bot.log"))


@app.get("/report")
def report() -> JSONResponse:
    return JSONResponse(read_report(Path("trade_log.csv")))
