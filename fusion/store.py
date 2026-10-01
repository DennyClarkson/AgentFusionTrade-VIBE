"""SQLite transactional profiles, immutable revisions and durable execution intents."""
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import DEFAULTS, validate


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS profiles(category TEXT,id TEXT,name TEXT,version INTEGER,data TEXT,updated_at REAL,PRIMARY KEY(category,id,version));
            CREATE TABLE IF NOT EXISTS active(category TEXT PRIMARY KEY,id TEXT);
            CREATE TABLE IF NOT EXISTS cycles(id TEXT PRIMARY KEY,created_at REAL,data TEXT);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at REAL,event TEXT,data TEXT);
            CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY,status TEXT,data TEXT,updated_at REAL);
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,data TEXT);
            CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,title TEXT,created_at REAL,updated_at REAL);
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,conversation_id TEXT,role TEXT,content TEXT,created_at REAL,metadata TEXT);
            CREATE TABLE IF NOT EXISTS agent_memory(id INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT,created_at REAL,data TEXT);
            """)
        for category, data in DEFAULTS.items():
            if not any(x["category"] == category for x in self.configs()):
                self.save(category, "default", "默认预设", data, 0)
                self.activate(category, "default")
        # Schema additions get a real revision, rather than silently changing old versions.
        for row in self.configs():
            normalized = validate(row["category"], row["data"])
            if normalized != row["data"]:
                self.save(row["category"], row["id"], row["name"], normalized, row["version"])

    @contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def configs(self):
        with self.db() as db:
            rows = db.execute("""SELECT p.*, CASE WHEN a.id=p.id THEN 1 ELSE 0 END AS active
              FROM profiles p LEFT JOIN active a ON p.category=a.category
              WHERE p.version=(SELECT MAX(q.version) FROM profiles q WHERE q.category=p.category AND q.id=p.id)
              ORDER BY p.category,p.id""").fetchall()
        return [{**dict(r), "data": json.loads(r["data"]), "active": bool(r["active"])} for r in rows]

    def active(self):
        rows = [x for x in self.configs() if x["active"]]
        return {x["category"]: validate(x["category"], x["data"]) for x in rows}, {x["category"]: {"id": x["id"], "version": x["version"]} for x in rows}

    def save(self, category, profile_id, name, data, expected_version):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", profile_id):
            raise ValueError("配置 ID 只能包含字母数字、下划线、短横线（1–64 字符）")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("配置名称不能为空且不能超过 100 字符")
        data = validate(category, data)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("SELECT COALESCE(MAX(version),0) FROM profiles WHERE category=? AND id=?", (category, profile_id)).fetchone()[0]
            if version != expected_version:
                raise Conflict("配置已更新，请刷新后重试")
            db.execute("INSERT INTO profiles VALUES(?,?,?,?,?,?)", (category, profile_id, name.strip(), version + 1, encode(data), time.time()))
            db.execute("INSERT INTO audit(created_at,event,data) VALUES(?,?,?)", (time.time(), "config.saved", encode({"category": category, "id": profile_id, "version": version + 1})))
        return next(x for x in self.configs() if x["category"] == category and x["id"] == profile_id)

    def activate(self, category, profile_id):
        with self.db() as db:
            if not db.execute("SELECT 1 FROM profiles WHERE category=? AND id=?", (category, profile_id)).fetchone():
                raise ValueError("配置不存在")
            db.execute("INSERT OR REPLACE INTO active VALUES(?,?)", (category, profile_id))
        self.audit("config.activated", {"category": category, "id": profile_id})
        return next(x for x in self.configs() if x["category"] == category and x["id"] == profile_id)

    def history(self, category, profile_id):
        with self.db() as db:
            rows = db.execute("SELECT * FROM profiles WHERE category=? AND id=? ORDER BY version DESC", (category, profile_id)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def cycle(self, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO cycles VALUES(?,?,?)", (value["id"], value["created_at"], encode(value)))

    def cycles(self, limit=30):
        with self.db() as db:
            rows = db.execute("SELECT data FROM cycles ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def count_cycles(self):
        with self.db() as db:
            return db.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]

    def latest_cycle(self, module):
        if module not in ("ai","ea"): raise ValueError("无效模块")
        with self.db() as db:
            row=db.execute("SELECT data FROM cycles WHERE COALESCE(json_extract(data,'$.module'),'ai')=? ORDER BY created_at DESC LIMIT 1",(module,)).fetchone()
        return json.loads(row[0]) if row else None

    def audit(self, event, data):
        with self.db() as db:
            db.execute("INSERT INTO audit(created_at,event,data) VALUES(?,?,?)", (time.time(), event, encode(data)))

    def audits(self, limit=50):
        with self.db() as db:
            rows = db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT data FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.put_many({key: value})

    def put_many(self, values):
        with self.db() as db:
            db.executemany("INSERT OR REPLACE INTO state VALUES(?,?)", [(k, encode(v)) for k, v in values.items()])

    def intent(self, key, value):
        with self.db() as db:
            try:
                db.execute("INSERT INTO intents VALUES(?,?,?,?)", (key, "pending", encode(value), time.time()))
            except sqlite3.IntegrityError:
                raise Conflict("该信号已有订单意图，禁止重复提交") from None

    def resolve(self, key, status, value, state_updates=None):
        with self.db() as db:
            db.execute("UPDATE intents SET status=?,data=?,updated_at=? WHERE id=?", (status, encode(value), time.time(), key))
            for state_key, data in (state_updates or {}).items():
                db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (state_key, encode(data)))

    def unresolved(self):
        with self.db() as db:
            rows = db.execute("SELECT * FROM intents WHERE status IN ('pending','unknown')").fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def new_conversation(self, title="EA 策略讨论"):
        key, now = uid(), time.time()
        with self.db() as db:
            db.execute("INSERT INTO conversations VALUES(?,?,?,?)", (key,title[:100],now,now))
        return {"id":key,"title":title[:100],"updated_at":now}

    def conversations(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 100")]

    def conversation(self, key):
        with self.db() as db:
            row = db.execute("SELECT * FROM conversations WHERE id=?", (key,)).fetchone()
            if not row: raise ValueError("对话不存在")
            messages = db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (key,)).fetchall()
        return {**dict(row),"messages":[{**dict(m),**json.loads(m["metadata"])} for m in messages]}

    def message(self, key, role, content, metadata=None):
        if role not in ("user","assistant","system"): raise ValueError("无效消息角色")
        now = time.time()
        with self.db() as db:
            if not db.execute("SELECT 1 FROM conversations WHERE id=?",(key,)).fetchone(): raise ValueError("对话不存在")
            cur = db.execute("INSERT INTO messages(conversation_id,role,content,created_at,metadata) VALUES(?,?,?,?,?)",(key,role,content,now,encode(metadata or {})))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?",(now,key))
        return cur.lastrowid

    def remember(self, scope, value):
        with self.db() as db:
            db.execute("INSERT INTO agent_memory(scope,created_at,data) VALUES(?,?,?)",(scope,time.time(),encode(value)))

    def memories(self, scope, limit=3, before=None):
        with self.db() as db:
            rows = db.execute("SELECT created_at,data FROM agent_memory WHERE scope=? AND created_at<? ORDER BY id DESC LIMIT ?",(scope,before or time.time()+1,max(0,min(40,limit)))).fetchall()
        return [{"recorded_at":r["created_at"],**json.loads(r["data"])} for r in reversed(rows)]


def uid():
    return uuid.uuid4().hex
