import sqlite3
from bot.capital_based_sizing_restore_patch import _configured_paper_sizing_base,_runtime_capital_size
from bot.authoritative_ledger import build_authoritative_ledger

def _conn():
    c=sqlite3.connect(":memory:");c.row_factory=sqlite3.Row;c.execute("CREATE TABLE paper_trades (id INTEGER PRIMARY KEY,user_id INTEGER,status TEXT,pnl REAL,net_pnl REAL,trading_mode TEXT,broker_name TEXT,entry_order_id TEXT,entry_price REAL,last_ltp REAL,qty INTEGER,capital_base REAL,created_at TEXT,exit_time TEXT)");return c

def _closed(c,p,t):c.execute("INSERT INTO paper_trades(user_id,status,pnl,net_pnl,trading_mode,entry_price,last_ltp,qty,created_at,exit_time) VALUES(1,'CLOSED',?,?,'paper',100,100,1,?,?)",(p,p,t,t));c.commit()
def test_profit_after_reset_increases_next_paper_sizing_base():
    c=_conn();_closed(c,5000,"2026-09-02 10:00:00");s={"trading_mode":"paper","paper_capital":20000,"paper_capital_reset_at":"2026-09-02 09:00:00"};assert _configured_paper_sizing_base(c,1,s)==25000;l=build_authoritative_ledger(c,1,s);assert l["current_capital"]==25000;c.close()
def test_loss_after_reset_reduces_next_paper_sizing_base():
    c=_conn();_closed(c,-5000,"2026-09-02 10:00:00");s={"trading_mode":"paper","paper_capital":20000,"paper_capital_reset_at":"2026-09-02 09:00:00"};assert _configured_paper_sizing_base(c,1,s)==15000;c.close()
def test_old_profit_is_ignored_after_new_capital_reset():
    c=_conn();_closed(c,30000,"2026-09-01 10:00:00");_closed(c,5000,"2026-09-02 10:00:00");s={"trading_mode":"paper","paper_capital":20000,"paper_capital_reset_at":"2026-09-02 09:00:00"};assert _configured_paper_sizing_base(c,1,s)==25000;c.close()
def test_runtime_quantity_uses_cycle_equity():
    assert _runtime_capital_size(20000,1,90,65,[],None)["qty"]==65;assert _runtime_capital_size(25000,1,90,65,[],None)["qty"]==130
def test_live_current_capital_uses_fresh_gateway_funds_when_flat():
    c=_conn();c.execute("CREATE TABLE live_broker_funds(user_id INTEGER PRIMARY KEY,available_cash REAL,used_margin REAL,total_limit REAL,broker TEXT,updated_at TEXT)");c.execute("INSERT INTO live_broker_funds VALUES(1,18000,2000,20000,'angelone',datetime('now'))");c.commit();l=build_authoritative_ledger(c,1,{"trading_mode":"live"});assert l["current_capital"]==20000 and l["broker_available_cash"]==18000;c.close()
