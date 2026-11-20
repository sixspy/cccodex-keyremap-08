"""实体键桥 backend.

正确性口径（全局唯一，禁止混用）—— 甲路径：先租后写（lease-before-write）。
=====================================================================
· 租约物化为 `leases` 表：scope_key + holder + lease_until + kind，可只读核证。
· 任何改生效窗（map_versions.status → 生效）或写正式区（formal_entities）的请求，
  必须先取得并持有对应 `version:<version>` scope 的租约；未取得则整笔拒绝，
  生效窗与正式区零改动，并写 contention_audit（争用审计）。
· 持租后，在同一事务内完成写入；写入前由 SQLite 触发器再次核验租约仍存活，
  即使绕过应用层也无法无租约改窗/写正式。
· 不使用乙路径（租约与写入同提交、失败再回滚已写数据）的任何写法。
· 切版与重映共用同一 scope 命名空间，两路不能同时对同一 scope 持租。
"""

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

PRODUCT = "实体键桥"
SKELETON = "实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账，切版/重映持租才可写"
CORRECTNESS_PATH = "A/lease-before-write"
LEASE_TTL_SECONDS = 60
LIVE_LEASE_SQL = "released_at IS NULL AND lease_until > strftime('%Y-%m-%dT%H:%M:%S','now')"


def now_s() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def today_s() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def _connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


@contextmanager
def connect():
    """只读/原子语句连接（自动提交模式）。"""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_tx():
    """显式写事务：BEGIN IMMEDIATE 保证同库写者串行，租约与写入不会互相穿插。"""
    conn = _connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        if conn.in_transaction:
            conn.execute("COMMIT")
    finally:
        conn.close()


def audit(conn: sqlite3.Connection, scope: str, actor: str, path: str, reason: str) -> None:
    conn.execute(
        "INSERT INTO contention_audit(scope_key,actor,path,reason,at) VALUES (?,?,?,?,?)",
        (scope, actor, path, reason, now_s()),
    )


def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u


app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS map_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version TEXT UNIQUE NOT NULL,
  status TEXT NOT NULL,
  effective_from TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mappings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version TEXT NOT NULL,
  old_key TEXT NOT NULL,
  new_key TEXT NOT NULL,
  kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS formal_entities(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_key TEXT NOT NULL,
  mapped_from TEXT NOT NULL,
  version TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conflicts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  old_key TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL
);
-- 租约物化表：scope_key + holder + lease_until + kind
CREATE TABLE IF NOT EXISTS leases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope_key TEXT NOT NULL,
  holder TEXT NOT NULL,
  lease_until TEXT NOT NULL,
  kind TEXT NOT NULL,
  token TEXT UNIQUE NOT NULL,
  acquired_at TEXT NOT NULL,
  released_at TEXT
);
-- 同一 scope 同时只允许一条存活租约；已释放/已过期行保留为可核证账目
CREATE UNIQUE INDEX IF NOT EXISTS ux_leases_live
  ON leases(scope_key) WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_leases_scope ON leases(scope_key, id);
CREATE TABLE IF NOT EXISTS contention_audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope_key TEXT NOT NULL,
  actor TEXT NOT NULL,
  path TEXT NOT NULL,
  reason TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_contentions_scope ON contention_audit(scope_key, id);
"""

# 第二道防线：绕过应用层直接写库也无法无租约改窗/写正式
TRIGGER_SQL = f"""
DROP TRIGGER IF EXISTS trg_map_versions_promote_lease;
CREATE TRIGGER trg_map_versions_promote_lease
BEFORE UPDATE OF status, effective_from ON map_versions
FOR EACH ROW
WHEN NEW.status = '生效'
BEGIN
  SELECT RAISE(ABORT, 'E_NO_LIVE_LEASE_WINDOW')
  WHERE NOT EXISTS (
    SELECT 1 FROM leases
     WHERE scope_key = 'version:' || NEW.version
       AND kind = 'cutover'
       AND {LIVE_LEASE_SQL}
  );
END;

DROP TRIGGER IF EXISTS trg_map_versions_demote_lease;
CREATE TRIGGER trg_map_versions_demote_lease
BEFORE UPDATE OF status ON map_versions
FOR EACH ROW
WHEN OLD.status = '生效' AND NEW.status <> '生效'
BEGIN
  -- 关窗同样算改生效窗：只有「另一版本已生效」才允许关窗（切版采用先升后降），
  -- 因此不可能把生效窗降成 0 个，也无法无租约地凭空关窗。
  SELECT RAISE(ABORT, 'E_NO_LIVE_LEASE_WINDOW')
  WHERE NOT EXISTS (
    SELECT 1 FROM map_versions v
     WHERE v.id <> OLD.id AND v.status = '生效'
  );
END;

DROP TRIGGER IF EXISTS trg_formal_entities_lease;
CREATE TRIGGER trg_formal_entities_lease
BEFORE INSERT ON formal_entities
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'E_NO_LIVE_LEASE_FORMAL')
  WHERE NOT EXISTS (
    SELECT 1 FROM leases
     WHERE scope_key = 'version:' || NEW.version
       AND kind = 'remap'
       AND {LIVE_LEASE_SQL}
  );
  SELECT RAISE(ABORT, 'E_VERSION_NOT_EFFECTIVE')
  WHERE (SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1) IS NOT NEW.version;
END;
"""


def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript(SCHEMA_SQL)
        if conn.execute("SELECT COUNT(*) c FROM map_versions").fetchone()["c"] == 0:
            conn.execute("INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)",
                         ("m-2026.11", "生效", "2026-11-01"))
            conn.execute("INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)",
                         ("m-2026.12", "候选", "2026-12-01"))
            conn.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)", [
                ("m-2026.11", "OLD-A1", "NEW-100", "一对一"),
                ("m-2026.11", "OLD-B1", "NEW-200", "多对一"),
                ("m-2026.11", "OLD-B2", "NEW-200", "多对一"),
                ("m-2026.12", "OLD-A1", "NEW-900", "一对一"),
            ])
            conn.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                         ("NEW-100", "OLD-A1", "m-2026.11", "2026-11-10T09:00:00"))
            conn.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", ("OLD-C9", "无映射", "挂起"))
        # 触发器在种子数据之后建立（种子行不属于租约管控的运行时写入）
        conn.executescript(TRIGGER_SQL)


@app.on_event("startup")
def _s(): init_db()


@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM mappings").fetchone()["c"]
        live = c.execute(f"SELECT COUNT(*) c FROM leases WHERE {LIVE_LEASE_SQL}").fetchone()["c"]
    return {
        "ok": True, "product": PRODUCT, "skeleton": SKELETON, "mappings": n,
        "db": str(DB_PATH), "correctness_path": CORRECTNESS_PATH, "live_leases": live,
    }


@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?",
                        (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16)
    SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


@app.get("/api/map-versions")
def versions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM map_versions ORDER BY id").fetchall()]}


@app.get("/api/mappings")
def mappings(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM mappings ORDER BY id").fetchall()]}


# ---------------------------------------------------------------- 租约
class LeaseIn(BaseModel):
    scope_key: str
    kind: str


def _scope_for(version: str) -> str:
    return f"version:{version}"


def _token_row(conn: sqlite3.Connection, token: str):
    return conn.execute(
        f"SELECT * FROM leases WHERE token=? AND {LIVE_LEASE_SQL}", (token,)
    ).fetchone()


@app.post("/api/leases/acquire")
def acquire_lease(body: LeaseIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    scope = body.scope_key.strip()
    if not scope.startswith("version:") or not scope[len("version:"):]:
        raise HTTPException(422, "scope_key 须为 version:<映射版本>")
    version = scope[len("version:"):]
    if body.kind not in ("cutover", "remap"):
        raise HTTPException(422, "kind 须为 cutover 或 remap")
    now = now_s()
    until = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_TTL_SECONDS)).strftime("%Y-%m-%dT%H:%M:%S")
    token = secrets.token_hex(16)
    rejected: HTTPException | None = None
    try:
        with write_tx() as c:
            if not c.execute("SELECT 1 FROM map_versions WHERE version=?", (version,)).fetchone():
                rejected = HTTPException(404, "映射版本不存在")
            else:
                # 过期未释放的行按到期时间封账，让出唯一索引名额（账目仍保留可查）
                c.execute(
                    "UPDATE leases SET released_at=lease_until "
                    "WHERE scope_key=? AND released_at IS NULL AND lease_until <= ?", (scope, now))
                live = c.execute(
                    f"SELECT * FROM leases WHERE scope_key=? AND {LIVE_LEASE_SQL}", (scope,)).fetchone()
                if live:
                    # 同一 scope 已有存活租约（无论持有人/kind）→ 后到者只留争用，不铸新令牌，
                    # 杜绝两路同时持同一 scope 各写一次
                    audit(c, scope, user, "lease",
                          f"租约争用：{live['holder']} 以 {live['kind']} 持有至 {live['lease_until']}")
                    rejected = HTTPException(409, "租约争用：同 scope 已有存活租约，后到请求只留争用审计")
                else:
                    c.execute("INSERT INTO leases(scope_key,holder,lease_until,kind,token,acquired_at,released_at) "
                              "VALUES (?,?,?,?,?,?,NULL)", (scope, user, until, body.kind, token, now))
    except sqlite3.IntegrityError:
        # 唯一存活租约索引兜底：并发下后到者的插入被库拒绝
        with write_tx() as c:
            audit(c, scope, user, "lease", "库级唯一存活租约索引拒绝并发获取")
        raise HTTPException(409, "租约争用：同 scope 存活租约由库级唯一索引拒绝")
    if rejected:
        raise rejected
    return {"scope_key": scope, "holder": user, "kind": body.kind,
            "lease_until": until, "token": token, "correctness_path": CORRECTNESS_PATH}


@app.get("/api/leases")
def list_leases(scope_key: str | None = None, active_only: bool = False,
                authorization: str | None = Header(default=None)):
    """只读核证接口：租约落库账目，绝不改库。"""
    user = require_user(authorization)
    now = now_s()
    sql = "SELECT * FROM leases"
    args: list = []
    where = []
    if scope_key:
        where.append("scope_key=?")
        args.append(scope_key)
    if active_only:
        where.append("released_at IS NULL AND lease_until > ?")
        args.append(now)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    with connect() as c:
        items = []
        for r in c.execute(sql, args).fetchall():
            d = dict(r)
            d["active"] = d["released_at"] is None and d["lease_until"] > now
            if d["holder"] != user:
                d["token"] = None  # 只向持有人出示 token
            items.append(d)
    return {"items": items, "now": now}


@app.get("/api/contentions")
def list_contentions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute(
            "SELECT * FROM contention_audit ORDER BY id DESC").fetchall()]}


class ReleaseIn(BaseModel):
    lease_token: str


@app.post("/api/leases/release")
def release_lease(body: ReleaseIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    with write_tx() as c:
        row = c.execute("SELECT * FROM leases WHERE token=?", (body.lease_token,)).fetchone()
        if not row:
            raise HTTPException(404, "租约不存在")
        if row["holder"] != user:
            raise HTTPException(403, "只能释放自己持有的租约")
        c.execute("UPDATE leases SET released_at=? WHERE id=? AND released_at IS NULL", (now_s(), row["id"]))
    return {"released": True, "scope_key": row["scope_key"], "token": body.lease_token}


# ---------------------------------------------------------------- 切版（先租后写）
class CutoverIn(BaseModel):
    version: str
    lease_token: str | None = None


@app.post("/api/cutover")
def cutover(body: CutoverIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    scope = _scope_for(body.version)
    if not body.lease_token:
        with write_tx() as c:
            audit(c, scope, user, "cutover", "无租约请求切版，整笔拒绝，生效窗与正式区零改动")
        raise HTTPException(409, "未持版本级租约：切版整笔拒绝，生效窗与正式区零改动，已留争用审计")
    reject: HTTPException | None = None
    result: dict | None = None
    try:
        with write_tx() as c:
            lease = _token_row(c, body.lease_token)
            target = c.execute("SELECT * FROM map_versions WHERE version=?", (body.version,)).fetchone()
            if not lease:
                audit(c, scope, user, "cutover", "租约无效或已过期，拒绝切版")
                reject = HTTPException(409, "租约无效或已过期：切版拒绝，已留争用审计")
            elif lease["scope_key"] != scope or lease["holder"] != user or lease["kind"] != "cutover":
                audit(c, scope, user, "cutover",
                      f"租约不符（scope={lease['scope_key']}, kind={lease['kind']}, holder={lease['holder']}）")
                reject = HTTPException(409, "租约 scope/kind/持有人与切版不符：整笔拒绝")
            elif not target:
                reject = HTTPException(404, "映射版本不存在")
            elif target["status"] == "生效":
                reject = HTTPException(409, "目标版本已是生效版本，无需切版")
            else:
                eff = today_s()
                # 先升后降：触发器要求关窗前必须已有另一版本生效
                c.execute("UPDATE map_versions SET status='生效', effective_from=? WHERE id=?",
                          (eff, target["id"]))
                for row in c.execute("SELECT id FROM map_versions WHERE status='生效' AND id<>?",
                                     (target["id"],)).fetchall():
                    c.execute("UPDATE map_versions SET status='历史' WHERE id=?", (row["id"],))
                c.execute("UPDATE leases SET released_at=? WHERE token=? AND released_at IS NULL",
                          (now_s(), body.lease_token))
                result = {"version": body.version, "status": "生效", "effective_from": eff,
                          "lease_scope": scope, "correctness_path": CORRECTNESS_PATH}
    except sqlite3.IntegrityError as e:
        with write_tx() as c:
            audit(c, scope, user, "cutover", f"库级租约守卫拒绝：{e}")
        raise HTTPException(409, f"库级租约守卫拒绝切版：{e}")
    if reject:
        raise reject
    return result


# ---------------------------------------------------------------- 重映闸（先租后写）
class RemapIn(BaseModel):
    old_key: str
    lease_token: str | None = None


@app.post("/api/remap")
def remap(body: RemapIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    with connect() as c:
        active = c.execute("SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()
    scope = _scope_for(active["version"]) if active else "version:<无>"
    if not body.lease_token:
        with write_tx() as c:
            audit(c, scope, user, "remap", "无租约请求重映，正式区零改动")
        raise HTTPException(409, "未持版本级租约：重映整笔拒绝，正式区零改动，已留争用审计")
    reject: HTTPException | None = None
    result: dict | None = None
    try:
        with write_tx() as c:
            lease = _token_row(c, body.lease_token)
            ver = c.execute("SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()
            if not lease:
                audit(c, scope, user, "remap", "租约无效或已过期，拒绝重映")
                reject = HTTPException(409, "租约无效或已过期：重映拒绝，正式区零改动，已留争用审计")
            elif not ver:
                reject = HTTPException(404, "无生效映射版本")
            else:
                expected = _scope_for(ver["version"])
                if lease["scope_key"] != expected or lease["holder"] != user or lease["kind"] != "remap":
                    audit(c, scope, user, "remap",
                          f"租约与当前生效版本不符（lease={lease['scope_key']}, active={expected}）")
                    reject = HTTPException(409, "租约 scope/kind/持有人与当前生效版本不符：整笔拒绝")
                else:
                    m = c.execute("SELECT * FROM mappings WHERE version=? AND old_key=?",
                                  (ver["version"], body.old_key)).fetchone()
                    if not m:
                        c.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)",
                                  (body.old_key, "无映射", "挂起"))
                        c.execute("UPDATE leases SET released_at=? WHERE token=? AND released_at IS NULL",
                                  (now_s(), body.lease_token))
                        reject = HTTPException(409, "无映射，已进冲突队列")
                    else:
                        # 触发器在此处核验 version:<active> 的 remap 租约仍存活且版本仍生效
                        c.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) "
                                  "VALUES (?,?,?,?)", (m["new_key"], body.old_key, ver["version"], now_s()))
                        c.execute("UPDATE leases SET released_at=? WHERE token=? AND released_at IS NULL",
                                  (now_s(), body.lease_token))
                        result = {"entity_key": m["new_key"], "version": ver["version"],
                                  "lease_scope": expected, "correctness_path": CORRECTNESS_PATH}
    except sqlite3.IntegrityError as e:
        with write_tx() as c:
            audit(c, scope, user, "remap", f"库级租约守卫拒绝：{e}")
        raise HTTPException(409, f"库级租约守卫拒绝重映：{e}")
    if reject:
        raise reject
    return result


@app.get("/api/formal")
def formal(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM formal_entities ORDER BY id DESC").fetchall()]}


@app.get("/api/conflicts")
def conflicts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM conflicts ORDER BY id DESC").fetchall()]}


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
