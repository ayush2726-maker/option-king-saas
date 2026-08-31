#!/usr/bin/env python3
"""OKAI local gateway V3: rate-safe Angel sync with broker-flat exit reconciliation."""

import time
from datetime import datetime

try:
    from . import okai_local_gateway as base
    from . import okai_local_gateway_v2 as v2
except ImportError:
    import okai_local_gateway as base
    import okai_local_gateway_v2 as v2

RATE_SAFE_VERSION = "1.3.3-FLAT-EXIT-RECONCILE"
LTP_MIN_INTERVAL_SECONDS = 2.2
RATE_LIMIT_BACKOFF_SECONDS = 8.0
FUNDS_REFRESH_SECONDS = 60.0
BROKER_POSITION_REFRESH_SECONDS = 5.0

class RateSafeAngelSession(base.AngelSession):
    def __init__(self, config):
        super().__init__(config)
        self._ltp_cache = {}; self._ltp_last_call = {}; self._ltp_backoff_until = {}
        self._position_cache = None; self._position_cache_at = 0.0

    @staticmethod
    def _key(exchange, symbol, token):
        return (str(exchange or "").upper(), str(symbol or ""), str(token or ""))

    @staticmethod
    def _is_rate_limit_error(exc):
        text = str(exc or "").lower()
        return any(x in text for x in ("exceeding access rate", "rate limit", "too many requests", "access denied"))

    def ltp(self, exchange, symbol, token):
        key=self._key(exchange,symbol,token); now=time.monotonic(); cached=self._ltp_cache.get(key)
        if cached is not None and (now < self._ltp_backoff_until.get(key,0) or now-self._ltp_last_call.get(key,0)<LTP_MIN_INTERVAL_SECONDS): return float(cached)
        self._ltp_last_call[key]=now
        try:
            response=self.login().ltpData(str(exchange),str(symbol),str(token))
            if not response or (isinstance(response,dict) and response.get("status") is False): raise RuntimeError(str(response)[:240])
            value=float(response["data"]["ltp"])
            if value<=0: raise RuntimeError("INVALID_OPTION_LTP")
            self._ltp_cache[key]=value; self._ltp_backoff_until[key]=0.0; return value
        except Exception as exc:
            if self._is_rate_limit_error(exc):
                self._ltp_backoff_until[key]=now+RATE_LIMIT_BACKOFF_SECONDS
                if cached is not None: return float(cached)
                raise RuntimeError(f"ANGEL_RATE_LIMIT_BACKOFF {RATE_LIMIT_BACKOFF_SECONDS:.0f}s | {str(exc)[:160]}")
            self.obj=None
            if cached is not None: return float(cached)
            raise

    def position_rows(self, force=False):
        now=time.monotonic()
        if not force and self._position_cache is not None and now-self._position_cache_at<BROKER_POSITION_REFRESH_SECONDS:
            return list(self._position_cache)
        response=self.login().position()
        if not isinstance(response,dict) or response.get("status") is False:
            raise RuntimeError(f"Angel position read failed: {str(response)[:220]}")
        rows=response.get("data") or []
        self._position_cache=list(rows); self._position_cache_at=now
        return list(rows)

    def net_position_quantity(self, exchange, symbol, token=""):
        wanted_symbol=str(symbol or "").upper(); wanted_exchange=str(exchange or "").upper(); wanted_token=str(token or "")
        matches=[]
        for row in self.position_rows():
            if str(row.get("tradingsymbol") or row.get("symbol") or "").upper()!=wanted_symbol: continue
            row_exchange=str(row.get("exchange") or row.get("exch_seg") or "").upper()
            if row_exchange and wanted_exchange and row_exchange!=wanted_exchange: continue
            row_token=str(row.get("symboltoken") or row.get("symbol_token") or "")
            if wanted_token and row_token and row_token!=wanted_token: continue
            matches.append(row)
        if not matches: return 0
        total=0
        for row in matches:
            raw=row.get("netqty", row.get("netQty", row.get("netquantity", row.get("netQuantity", row.get("quantity",0)))))
            try: total+=int(float(raw or 0))
            except Exception: pass
        return total

    def latest_completed_sell(self, exchange, symbol, token="", quantity=0):
        """Return broker-confirmed completed SELL average for this exact contract."""
        response=self.login().orderBook()
        if not isinstance(response,dict) or response.get("status") is False: return None
        rows=response.get("data") or []; wanted_symbol=str(symbol or "").upper(); wanted_exchange=str(exchange or "").upper(); wanted_token=str(token or ""); wanted_qty=int(quantity or 0)
        matches=[]
        for idx,row in enumerate(rows):
            if str(row.get("transactiontype") or row.get("transactionType") or "").upper()!="SELL": continue
            status=str(row.get("orderstatus") or row.get("status") or "").lower()
            if status not in {"complete","completed","filled"}: continue
            if str(row.get("tradingsymbol") or row.get("symbol") or "").upper()!=wanted_symbol: continue
            row_exchange=str(row.get("exchange") or row.get("exch_seg") or "").upper()
            if row_exchange and wanted_exchange and row_exchange!=wanted_exchange: continue
            row_token=str(row.get("symboltoken") or row.get("symbol_token") or "")
            if wanted_token and row_token and row_token!=wanted_token: continue
            try: row_qty=int(float(row.get("filledshares") or row.get("filledquantity") or row.get("quantity") or 0))
            except Exception: row_qty=0
            if wanted_qty>0 and row_qty>0 and row_qty!=wanted_qty: continue
            try: avg=float(row.get("averageprice") or row.get("averagePrice") or row.get("price") or 0)
            except Exception: avg=0.0
            if avg<=0: continue
            stamp=str(row.get("exchtime") or row.get("updatetime") or row.get("exchangeupdatetime") or row.get("ordertime") or "")
            matches.append((stamp,idx,avg,str(row.get("orderid") or "")))
        if not matches: return None
        matches.sort(key=lambda x:(x[0],x[1]))
        stamp,_,avg,order_id=matches[-1]
        return {"price":avg,"order_id":order_id,"time":stamp}

class RateSafeSaaSClient(v2.RiskV2SaaSClient):
    def __init__(self, config):
        super().__init__(config); self._funds_angel=RateSafeAngelSession(config); self._funds_cache=None; self._funds_cache_at=0.0
    def _broker_funds(self):
        now=time.monotonic()
        if self._funds_cache is not None and now-self._funds_cache_at<FUNDS_REFRESH_SECONDS: return dict(self._funds_cache)
        result=super()._broker_funds()
        if result is not None: self._funds_cache=dict(result); self._funds_cache_at=now; return result
        return dict(self._funds_cache) if self._funds_cache is not None else None

class RateSafeGatewayRunner(v2.RiskV2GatewayRunner):
    def __init__(self, config):
        super().__init__(config); self.angel=RateSafeAngelSession(config)
        self._replay_flat_syncs()

    def _exit_event(self, position, reason="BROKER_POSITION_ALREADY_FLAT"):
        fill=None
        try: fill=self.angel.latest_completed_sell(position["exchange"],position["symbol"],position["symboltoken"],position["quantity"])
        except Exception as exc: print(f"⚠️ Broker exit fill lookup pending | trade={position['trade_id']} | {str(exc)[:140]}")
        if fill and float(fill.get("price") or 0)>0:
            exit_price=float(fill["price"]); exit_order_id=str(fill.get("order_id") or "BROKER_FLAT"); exit_time=str(fill.get("time") or base.now_iso())
        else:
            exit_price=float(position["exit_price"] or position["last_ltp"] or 0); exit_order_id=str(position["exit_order_id"] or "BROKER_FLAT"); exit_time=str(position["closed_at"] or base.now_iso())
        entry=float(position["entry_price"] or 0); qty=int(position["quantity"] or 0); pnl=round((exit_price-entry)*qty,2) if exit_price>0 and entry>0 and qty>0 else 0.0
        return {"event":"EXIT_FILLED","trade_id":int(position["trade_id"]),"symbol":str(position["symbol"]),"symboltoken":str(position["symboltoken"]),"exchange":str(position["exchange"]),"entry_order_id":str(position["entry_order_id"] or ""),"broker_order_id":exit_order_id,"entry_price":entry,"quantity":qty,"ltp":exit_price,"exit_price":exit_price,"pnl":pnl,"reason":reason,"exit_time":exit_time,"local_status":"closed","risk_engine":RATE_SAFE_VERSION}

    def _replay_flat_syncs(self):
        try:
            rows=self.db.execute("SELECT * FROM local_positions WHERE status='closed' AND exit_reason='BROKER_POSITION_ALREADY_FLAT' ORDER BY trade_id DESC LIMIT 10").fetchall()
            for position in rows:
                event=self._exit_event(position)
                self.notify_position_event(event)
                print(f"🔁 FLAT EXIT REPLAY | trade={position['trade_id']} | {position['symbol']} | exit={event['exit_price']:.2f}")
        except Exception as exc:
            print(f"⚠️ Flat exit replay pending | {str(exc)[:160]}")

    def _mark_broker_flat(self, position, reason="BROKER_POSITION_ALREADY_FLAT"):
        event=self._exit_event(position,reason); exit_price=float(event.get("exit_price") or 0)
        self.db.execute("UPDATE local_positions SET status='closed', closed_at=?, exit_order_id=?, exit_price=?, exit_reason=? WHERE trade_id=?",(base.now_iso(),str(event.get("broker_order_id") or "BROKER_FLAT"),exit_price if exit_price>0 else None,str(reason),int(position["trade_id"])))
        self.db.commit(); self.notify_position_event(event)
        print(f"🧹 BROKER FLAT SYNC | trade={position['trade_id']} | {position['symbol']} | exit={exit_price:.2f} | duplicate SELL blocked")
        return event

    def execute_exit(self, trade_id, reason, command=None):
        position=self.db.execute("SELECT * FROM local_positions WHERE trade_id=? AND status IN ('open','exit_submitted')",(int(trade_id),)).fetchone()
        if not position: raise RuntimeError("Local open position not found")
        try: net_qty=self.angel.net_position_quantity(position["exchange"],position["symbol"],position["symboltoken"])
        except Exception as exc: raise RuntimeError(f"EXIT_BLOCKED_BROKER_POSITION_UNVERIFIED: {str(exc)[:180]}")
        if net_qty<=0:
            result=self._mark_broker_flat(position)
            if command is not None: self.remember_command(command["id"],command["action"],"succeeded",result)
            return result
        requested=int(position["quantity"] or 0)
        if requested<=0: raise RuntimeError("EXIT_BLOCKED_INVALID_LOCAL_QUANTITY")
        if net_qty<requested: raise RuntimeError(f"EXIT_BLOCKED_QTY_MISMATCH: local={requested} broker={net_qty}")
        return super().execute_exit(trade_id,reason,command)

    def monitor_positions(self):
        now_ist=datetime.now(base.IST); current_hhmm=now_ist.strftime("%H:%M")
        for position in self.open_positions():
            try:
                net_qty=self.angel.net_position_quantity(position["exchange"],position["symbol"],position["symboltoken"])
                if net_qty<=0: self._mark_broker_flat(position); continue
                ltp=self.angel.ltp(position["exchange"],position["symbol"],position["symboltoken"])
                entry=float(position["entry_price"]); initial_sl=float(position["initial_sl_price"] or position["sl_price"]); peak=max(float(position["peak_ltp"] or entry),float(ltp)); cost_be=float(position["breakeven_price"] or entry)
                trail=v2.dynamic_profit_lock(entry,initial_sl,peak,cost_be); old_sl=float(position["sl_price"] or initial_sl); active_sl=max(old_sl,float(trail["sl_price"])); stage=trail["stage"]
                self.db.execute("UPDATE local_positions SET last_ltp=?, peak_ltp=?, sl_price=?, trail_stage=? WHERE trade_id=?",(ltp,peak,active_sl,stage,position["trade_id"])); self.db.commit()
                reason=None
                if ltp<=active_sl: reason="PROFIT_LOCK_TRAIL" if stage!="INITIAL_ATR_SL" else "LOCAL ATR SL HIT"
                elif current_hhmm>=str(position["force_exit_at"]): reason="LOCAL EOD EXIT 15:25 IST"
                if reason: self.execute_exit(position["trade_id"],reason); continue
                last_sent=self.last_position_heartbeat.get(position["trade_id"],0)
                if base.time.time()-last_sent>=10:
                    event={"event":"POSITION_HEARTBEAT","trade_id":int(position["trade_id"]),"symbol":str(position["symbol"]),"symboltoken":str(position["symboltoken"]),"exchange":str(position["exchange"]),"option_type":str(position["option_type"] or ""),"entry_order_id":str(position["entry_order_id"] or ""),"entry_price":entry,"quantity":int(position["quantity"]),"ltp":float(ltp),"peak_ltp":peak,"active_sl":active_sl,"cost_safe_breakeven":cost_be,"trail_stage":stage,"peak_r":trail["peak_r"],"risk_engine":RATE_SAFE_VERSION,"local_status":"open"}
                    self.saas.position_event(event); self.last_position_heartbeat[position["trade_id"]]=base.time.time(); print(f"📡 POSITION_SYNC | {position['symbol']} | ltp={ltp:.2f} | qty={int(position['quantity'])}")
            except Exception as exc: print(f"⚠️ Position monitor warning | trade={position['trade_id']} | {str(exc)[:180]}")

def install_patches():
    v2.install_patches(); base.AGENT_VERSION=RATE_SAFE_VERSION; base.SaaSClient=RateSafeSaaSClient; base.GatewayRunner=RateSafeGatewayRunner

def command_doctor_v3():
    install_patches(); v2.command_doctor_v2(); print("Angel LTP rate-safe polling: ENABLED ✅"); print("Broker-flat duplicate SELL guard: ENABLED ✅"); print("Broker exit fill reconciliation: ENABLED ✅"); print("Closed-flat replay sync: ENABLED ✅"); print("Direct symbol position sync: ENABLED ✅"); print(f"Gateway version: {RATE_SAFE_VERSION} ✅")

def main():
    install_patches(); base.command_doctor=command_doctor_v3; base.main()

if __name__ == "__main__": main()
