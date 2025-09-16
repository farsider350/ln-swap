from flask import Flask, request, render_template, redirect, url_for
import sqlite3
import subprocess
import json
import time
import os
from decimal import Decimal

app = Flask(__name__)
CFG = {}
DB_PATH = "swap.db"

def load_config():
    global CFG
    with open("config.json", "r") as f:
        CFG = json.load(f)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
      CREATE TABLE IF NOT EXISTS swaps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ln_invoice TEXT,
        onchain_address TEXT,
        expected_sats INTEGER,
        fee_sats INTEGER,
        status TEXT,        -- "waiting_onchain", "confirmed", "paid", "failed"
        txid TEXT,           -- onchain txid
        ln_payment_txid TEXT,
        created_at INTEGER,
        updated_at INTEGER
      )
    """)
    conn.commit()
    conn.close()

def run_bitcoin_rpc(method, params=None):
    if params is None:
        params = []
    rpc = CFG["bitcoin_rpc"]
    cmd = [
        "bitcoin-cli",
        "-rpcuser=" + rpc["user"],
        "-rpcpassword=" + rpc["password"],
        "-rpcport=" + str(rpc["port"]),
        "-rpcconnect=" + rpc["host"],
        method
    ] + params
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception("bitcoin rpc error: " + result.stderr)
    return json.loads(result.stdout)

def run_cln_cli(args_list):
    cmd = [CFG["cln_cli_path"]] + args_list
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception("CLN CLI error: " + result.stderr)
    # CLN CLI output may not always be JSON—depending on command. We assume JSON.
    return json.loads(result.stdout)

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        ln_invoice = request.form["ln_invoice"].strip()
        expected_sats = int(request.form["expected_sats"])
        if expected_sats < CFG["min_btc_sats"]:
            return "Amount too small", 400

        # fee
        fee = int(expected_sats * CFG["swap_fee_percent"])
        payout_sats = expected_sats - fee

        # generate onchain address
        addr = run_bitcoin_rpc("getnewaddress", ["bech32"])
        now = int(time.time())
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
          INSERT INTO swaps (ln_invoice, onchain_address, expected_sats, fee_sats, status, created_at, updated_at)
          VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (ln_invoice, addr, expected_sats, fee, "waiting_onchain", now, now))
        swap_id = c.lastrowid
        conn.commit()
        conn.close()

        return redirect(url_for("status", swap_id=swap_id))
    return render_template("index.html")

@app.route("/status/<int:swap_id>")
def status(swap_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, ln_invoice, onchain_address, expected_sats, fee_sats, status, txid, ln_payment_txid FROM swaps WHERE id = ?", (swap_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return "Swap not found", 404
    swap = {
        "id": row[0],
        "ln_invoice": row[1],
        "onchain_address": row[2],
        "expected_sats": row[3],
        "fee_sats": row[4],
        "status": row[5],
        "txid": row[6],
        "ln_payment_txid": row[7]
    }
    return render_template("status.html", swap=swap)

def monitor_onchain_and_pay():
    """Background job: check all "waiting_onchain" swaps, see if funds arrived, confirm, then pay LN."""
    while True:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, onchain_address, expected_sats, ln_invoice, status FROM swaps WHERE status = 'waiting_onchain'")
        rows = c.fetchall()
        for (swap_id, address, expected_sats, ln_invoice, status) in rows:
            # list received funds to address
            # bitcoin-cli listunspent might help
            utxos = run_bitcoin_rpc("listunspent", ["1", 9999999, [address]])
            total = sum([int(utxo["amount"] * Decimal(100_000_000)) for utxo in utxos])
            if total >= expected_sats:
                # mark confirmed
                now = int(time.time())
                c.execute("UPDATE swaps SET status = ?, txid = ?, updated_at = ? WHERE id = ?", ("confirmed", utxos[0]["txid"], now, swap_id))
                conn.commit()

                # pay the LN invoice
                try:
                    pay_result = run_cln_cli(["pay", ln_invoice])
                    ln_txid = pay_result.get("payment_hash") or pay_result.get("payment_preimage") or pay_result.get("id") or json.dumps(pay_result)
                    # update
                    now2 = int(time.time())
                    c.execute("UPDATE swaps SET status = ?, ln_payment_txid = ?, updated_at = ? WHERE id = ?", ("paid", ln_txid, now2, swap_id))
                    conn.commit()
                except Exception as e:
                    now3 = int(time.time())
                    c.execute("UPDATE swaps SET status = ?, updated_at = ? WHERE id = ?", ("failed", now3, swap_id))
                    conn.commit()
        conn.close()
        time.sleep(10)

if __name__ == "__main__":
    load_config()
    init_db()
    # start monitor in background thread
    from threading import Thread
    t = Thread(target=monitor_onchain_and_pay, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5000)
