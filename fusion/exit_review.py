"""Durable, account-scoped SL/TP exit-fill review queue. Contains no order path."""
import hashlib
import json
import time

from .store import Conflict, encode


class ExitReviewQueue:
    def __init__(self, store):
        self.store = store
        # Recovery records work, but never starts a model or an execution session.
        with store.db() as db:
            for row in db.execute("SELECT scope,ticket,data FROM ea_exit_events WHERE status='running'").fetchall():
                event = json.loads(row["data"])
                event.update(status="pending", next_attempt_at=0, error="上次复盘被中断，等待管理会话重新启动")
                db.execute("UPDATE ea_exit_events SET status='pending',data=? WHERE scope=? AND ticket=?", (encode(event), row["scope"], row["ticket"]))

    @staticmethod
    def scope(cfg, account):
        value = [account["login"], account["server"], cfg["ea"]["execution_magic"], cfg["ea"]["strategy"]["symbol"]]
        return hashlib.sha256(encode(value).encode()).hexdigest()

    def begin(self, scope, now=None):
        now = int(time.time() if now is None else now)  # MT5 history query boundaries can round to seconds.
        key = "ea_exit_watch:"+scope
        with self.store.db() as db:
            db.execute("INSERT OR IGNORE INTO state(key,data) VALUES(?,?)", (key, encode({"since": now, "scanned_until": now})))
            return json.loads(db.execute("SELECT data FROM state WHERE key=?", (key,)).fetchone()[0])

    def record(self, scope, cfg, versions, snapshot):
        """Insert discovery and advance the scan checkpoint in one transaction."""
        now = snapshot["captured_at"]
        if self.scope(cfg, snapshot["account"]) != scope:
            raise Conflict("成交快照账户或策略范围已变化")
        with self.store.db() as db:
            watch = json.loads(db.execute("SELECT data FROM state WHERE key=?", ("ea_exit_watch:"+scope,)).fetchone()[0])
            for deal in sorted(snapshot["deals"], key=lambda d: (d["time_msc"], d["ticket"])):
                if deal.get("exit_reason") not in {"sl", "tp"} or deal.get("magic") != cfg["ea"]["execution_magic"] or deal.get("symbol") != cfg["ea"]["strategy"]["symbol"]:
                    continue
                stamp = deal["time_msc"]/1000
                if stamp < watch["since"]:
                    continue  # First activation does not replay the account's old history.
                if stamp > now+60:
                    raise ValueError("成交时间晚于当前 UTC，请核对 MT5 原始时间校正")
                event = {"scope": scope, "ticket": str(deal["ticket"]), "reason": deal["exit_reason"], "deal": deal,
                         "account": {k: snapshot["account"][k] for k in ("login", "server")}, "observed_at": now,
                         "observed_config_versions": versions, "status": "pending", "attempts": 0, "next_attempt_at": 0}
                db.execute("INSERT OR IGNORE INTO ea_exit_events VALUES(?,?,?,?,?)", (scope, event["ticket"], "pending", stamp, encode(event)))
            watch["scanned_until"] = max(watch["scanned_until"], now)
            db.execute("UPDATE state SET data=? WHERE key=?", (encode(watch), "ea_exit_watch:"+scope))

    def next(self, scope):
        with self.store.db() as db:
            row = db.execute("SELECT data FROM ea_exit_events WHERE scope=? AND status IN ('pending','error') AND json_extract(data,'$.next_attempt_at')<=? ORDER BY occurred_at,ticket LIMIT 1", (scope, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    def claim(self, event, job):
        with self.store.db() as db:
            row = db.execute("SELECT data FROM ea_exit_events WHERE scope=? AND ticket=?", (event["scope"], event["ticket"])).fetchone()
            current = json.loads(row[0]) if row else None
            if not current or current["status"] not in {"pending", "error"} or current["next_attempt_at"] > time.time():
                raise Conflict("该成交复盘已在处理或等待重试")
            current.update(status="running", attempts=current["attempts"]+1, job_id=job["id"])
            job["trigger"] = {"kind": "protection_exit", **current}
            db.execute("UPDATE ea_exit_events SET status='running',data=? WHERE scope=? AND ticket=?", (encode(current), event["scope"], event["ticket"]))
            for key, value in {"ea_latest_job": job["id"], "ea_job:"+job["id"]: job}.items():
                db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, encode(value)))

    def finish(self, job, retry_seconds):
        event = job["trigger"]
        with self.store.db() as db:
            row = db.execute("SELECT data FROM ea_exit_events WHERE scope=? AND ticket=?", (event["scope"], event["ticket"])).fetchone()
            current = json.loads(row[0])
            if current.get("job_id") != job["id"]:
                raise Conflict("成交复盘任务归属已变化")
            state = "completed" if job["status"] == "completed" else "pending" if job["status"] == "cancelled" else "error"
            delay = retry_seconds * min(2**min(current["attempts"]-1, 5), 30)
            current.update(status=state, finished_at=time.time(), error=job.get("error"), next_attempt_at=time.time()+delay)
            db.execute("UPDATE ea_exit_events SET status=?,data=? WHERE scope=? AND ticket=?", (state, encode(current), event["scope"], event["ticket"]))
            db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", ("ea_job:"+job["id"], encode(job)))

    def summary(self, scope):
        if not scope:
            return {"pending": 0, "running": 0, "error": 0, "completed": 0, "recent": []}
        with self.store.db() as db:
            counts = {r[0]: r[1] for r in db.execute("SELECT status,COUNT(*) FROM ea_exit_events WHERE scope=? GROUP BY status", (scope,))}
            rows = db.execute("SELECT data FROM ea_exit_events WHERE scope=? ORDER BY occurred_at DESC,ticket DESC LIMIT 5", (scope,)).fetchall()
        recent = []
        for row in rows:
            item = json.loads(row[0])
            recent.append({k: item.get(k) for k in ("ticket", "reason", "status", "attempts", "job_id", "next_attempt_at", "error")})
        return {**{k: counts.get(k, 0) for k in ("pending", "running", "error", "completed")}, "recent": recent}
