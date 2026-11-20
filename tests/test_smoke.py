
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import app, init_db

ADMIN = {"username": "admin", "password": "123456"}
VIEWER = {"username": "viewer", "password": "view123"}


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield tmp_path / "t.db"


@pytest.fixture
def client():
    return TestClient(app)


def auth(client, creds=ADMIN):
    tok = client.post("/api/login", json=creds).json()["token"]
    return {"Authorization": "Bearer " + tok}


def formal_count(client, h):
    return len(client.get("/api/formal", headers=h).json()["items"])


def versions(client, h):
    return {v["version"]: v for v in client.get("/api/map-versions", headers=h).json()["items"]}


def leases(client, h, **params):
    return client.get("/api/leases", headers=h, params=params).json()["items"]


def contentions(client, h):
    return client.get("/api/contentions", headers=h).json()["items"]


def acquire(client, h, version, kind):
    return client.post("/api/leases/acquire", headers=h,
                       json={"scope_key": f"version:{version}", "kind": kind})


# ---------------------------------------------------------------- 夹具 1：无租约零写
def test_remap_without_lease_writes_nothing(client):
    h = auth(client)
    before = formal_count(client, h)
    r = client.post("/api/remap", headers=h, json={"old_key": "OLD-B2"})
    assert r.status_code == 409
    assert "零改动" in r.json()["detail"]
    assert formal_count(client, h) == before
    # 冲突队列也不因被拒请求增量
    assert not any(c["old_key"] == "OLD-B2" for c in client.get("/api/conflicts", headers=h).json()["items"])
    assert any(c["path"] == "remap" and "无租约" in c["reason"] for c in contentions(client, h))


def test_cutover_without_lease_leaves_window_and_formal_untouched(client):
    h = auth(client)
    before_versions = versions(client, h)
    before_formal = formal_count(client, h)
    r = client.post("/api/cutover", headers=h, json={"version": "m-2026.12"})
    assert r.status_code == 409
    assert versions(client, h) == before_versions
    assert before_versions["m-2026.12"]["status"] == "候选"
    assert formal_count(client, h) == before_formal
    assert any(c["path"] == "cutover" and "无租约" in c["reason"] for c in contentions(client, h))


def test_expired_or_foreign_lease_token_rejected(client):
    h = auth(client)
    lease = acquire(client, h, "m-2026.11", "remap").json()
    before = formal_count(client, h)
    # 释放后原 token 失效
    assert client.post("/api/leases/release", headers=h, json={"lease_token": lease["token"]}).status_code == 200
    r = client.post("/api/remap", headers=h, json={"old_key": "OLD-B1", "lease_token": lease["token"]})
    assert r.status_code == 409
    assert formal_count(client, h) == before

    # 别人持有的 lease token 不可用
    viewer = auth(client, VIEWER)
    foreign = acquire(client, viewer, "m-2026.11", "remap").json()
    admin = auth(client)
    r = client.post("/api/remap", headers=admin, json={"old_key": "OLD-B1", "lease_token": foreign["token"]})
    assert r.status_code == 409
    assert formal_count(client, admin) == before


# ---------------------------------------------------------------- 夹具 2：持租成功可查证
def test_held_lease_is_materialized_and_queryable(client):
    h = auth(client)
    r = acquire(client, h, "m-2026.12", "cutover")
    assert r.status_code == 200
    body = r.json()
    assert body["scope_key"] == "version:m-2026.12"
    assert body["kind"] == "cutover"
    assert body["correctness_path"] == "A/lease-before-write"

    items = leases(client, h, scope_key="version:m-2026.12", active_only=True)
    assert len(items) == 1
    row = items[0]
    assert row["holder"] == "admin"
    assert row["lease_until"] > row["acquired_at"]
    assert row["released_at"] is None and row["active"] is True
    assert row["token"] == body["token"]

    # 非持有人只读口径看不到 token，但能看到租约存在
    v = leases(client, auth(client, VIEWER), scope_key="version:m-2026.12")
    assert v and v[0]["token"] is None and v[0]["holder"] == "admin"

    # 切版成功后租约随同事务释放，账目仍可查
    r = client.post("/api/cutover", headers=h, json={"version": "m-2026.12", "lease_token": body["token"]})
    assert r.status_code == 200
    assert versions(client, h)["m-2026.12"]["status"] == "生效"
    assert versions(client, h)["m-2026.11"]["status"] == "历史"
    hist = leases(client, h, scope_key="version:m-2026.12")
    assert hist[0]["released_at"] is not None and hist[0]["active"] is False


def test_remap_with_lease_lands_in_formal_once(client):
    h = auth(client)
    before = formal_count(client, h)
    lease = acquire(client, h, "m-2026.11", "remap").json()
    r = client.post("/api/remap", headers=h, json={"old_key": "OLD-B2", "lease_token": lease["token"]})
    assert r.status_code == 200 and r.json()["entity_key"] == "NEW-200"
    assert formal_count(client, h) == before + 1
    # 同一租约已随写入释放，重放同 token 不再增量
    r = client.post("/api/remap", headers=h, json={"old_key": "OLD-B2", "lease_token": lease["token"]})
    assert r.status_code == 409
    assert formal_count(client, h) == before + 1


def test_lease_kind_mismatch_rejected(client):
    h = auth(client)
    before = formal_count(client, h)
    lease = acquire(client, h, "m-2026.11", "remap").json()
    r = client.post("/api/cutover", headers=h, json={"version": "m-2026.11", "lease_token": lease["token"]})
    assert r.status_code == 409
    assert versions(client, h)["m-2026.11"]["status"] == "生效"
    assert formal_count(client, h) == before


# ---------------------------------------------------------------- 夹具 3：两路争用同 scope
def test_second_caller_same_scope_only_leaves_contention(client):
    h = auth(client)
    first = acquire(client, h, "m-2026.12", "cutover")
    assert first.status_code == 200
    second = acquire(client, h, "m-2026.12", "remap")
    assert second.status_code == 409
    assert "争用" in second.json()["detail"]

    # 后到者不应拿到 token，存活租约仍唯一
    live = leases(client, h, scope_key="version:m-2026.12", active_only=True)
    assert len(live) == 1
    assert live[0]["token"] == first.json()["token"]
    # 后到者不带/带假 token 去写都不能增量
    before = formal_count(client, h)
    assert client.post("/api/remap", headers=h, json={"old_key": "OLD-A1"}).status_code == 409
    assert client.post("/api/remap", headers=h,
                       json={"old_key": "OLD-A1", "lease_token": first.json()["token"]}).status_code == 409
    assert formal_count(client, h) == before
    assert any(c["path"] == "lease" and "争用" in c["reason"] for c in contentions(client, h))


def test_cutover_and_remap_cannot_hold_same_scope_concurrently(client):
    h = auth(client)
    assert acquire(client, h, "m-2026.11", "remap").status_code == 200
    assert acquire(client, h, "m-2026.11", "cutover").status_code == 409
    # 另一版本 scope 不受影响
    assert acquire(client, h, "m-2026.12", "cutover").status_code == 200


# ---------------------------------------------------------------- 库级守卫（绕过 API）
def test_triggers_block_direct_db_writes(client, _db):
    conn = sqlite3.connect(_db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="E_NO_LIVE_LEASE_FORMAL"):
            conn.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) "
                         "VALUES ('NEW-X','OLD-X','m-2026.11','2026-11-20T00:00:00')")
        with pytest.raises(sqlite3.IntegrityError, match="E_NO_LIVE_LEASE_WINDOW"):
            conn.execute("UPDATE map_versions SET status='生效' WHERE version='m-2026.12'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE map_versions SET status='历史' WHERE version='m-2026.11'")
    finally:
        conn.close()


def test_remap_to_non_effective_version_rejected(client, _db):
    h = auth(client)
    # 候选版本即使持有 remap 租约，写正式仍被 E_VERSION_NOT_EFFECTIVE 拒绝
    acquire(client, h, "m-2026.12", "remap")
    before = formal_count(client, h)
    conn = sqlite3.connect(_db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="E_VERSION_NOT_EFFECTIVE"):
            conn.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) "
                         "VALUES ('NEW-900','OLD-A1','m-2026.12','2026-11-20T00:00:00')")
    finally:
        conn.close()
    # 应用层同样拒绝（scope 与当前生效版本不符）
    assert client.post("/api/remap", headers=h, json={"old_key": "OLD-A1"}).status_code == 409
    assert formal_count(client, h) == before


# ---------------------------------------------------------------- 真并发：两路同时抢同 scope
def test_concurrent_acquire_only_one_wins(client, _db):
    import threading

    h = auth(client)
    results: list[int] = []
    barrier = threading.Barrier(8)

    def worker(kind):
        barrier.wait()
        r = acquire(client, h, "m-2026.12", kind)
        results.append(r.status_code)

    threads = [threading.Thread(target=worker, args=("cutover" if i % 2 == 0 else "remap",))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1, results
    assert results.count(409) == 7, results
    live = leases(client, h, scope_key="version:m-2026.12", active_only=True)
    assert len(live) == 1
    # 7 次后到全部留争用审计
    con = [c for c in contentions(client, h) if c["path"] == "lease"]
    assert len(con) == 7


def test_concurrent_remap_and_cutover_cannot_both_write(client, _db):
    import threading

    h = auth(client)
    before = formal_count(client, h)
    lease = acquire(client, h, "m-2026.11", "remap").json()
    results: dict[str, int] = {}
    barrier = threading.Barrier(2)

    def do_remap():
        barrier.wait()
        results["remap"] = client.post(
            "/api/remap", headers=h, json={"old_key": "OLD-B1", "lease_token": lease["token"]}).status_code

    def do_cutover():
        barrier.wait()
        results["cutover"] = client.post(
            "/api/cutover", headers=h, json={"version": "m-2026.12", "lease_token": lease["token"]}).status_code

    ts = [threading.Thread(target=do_remap), threading.Thread(target=do_cutover)]
    for t in ts: t.start()
    for t in ts: t.join()
    # 同一 token 只能兑付一次：租约 kind=remap 且已随首个提交释放，另一路必被拒
    assert results["remap"] == 200
    assert results["cutover"] == 409
    assert formal_count(client, h) == before + 1
    assert versions(client, h)["m-2026.11"]["status"] == "生效"


def test_cutover_cannot_leave_zero_effective_versions(client, _db):
    h = auth(client)
    # 直接库级尝试把唯一生效版本降级（无替代版本）→ 触发器拒绝
    conn = sqlite3.connect(_db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="E_NO_LIVE_LEASE_WINDOW"):
            conn.execute("UPDATE map_versions SET status='历史' WHERE version='m-2026.11'")
    finally:
        conn.close()
    # 正规切版后仍恰好一个生效版本
    lease = acquire(client, h, "m-2026.12", "cutover").json()
    assert client.post("/api/cutover", headers=h,
                       json={"version": "m-2026.12", "lease_token": lease["token"]}).status_code == 200
    eff = [v["version"] for v in versions(client, h).values() if v["status"] == "生效"]
    assert eff == ["m-2026.12"]
