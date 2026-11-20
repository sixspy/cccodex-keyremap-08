"""本地夹具（SQLite）——不依赖 pytest，直接跑：

    python fixtures/lease_race_fixture.py

用临时 SQLite 库验证三道口径：
  F1 无租约请求：正式区零增。
  F2 持租成功：租约落库、可通过只读接口核证，写入随同事务释放租约。
  F3 两路同时抢同 scope：后到只留争用审计，正式区零增。
"""

import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import app as A  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

A.DB_PATH = Path(tempfile.mkdtemp()) / "fixture.db"
A.init_db()
client = TestClient(A.app)
H = {"Authorization": "Bearer " + client.post(
    "/api/login", json={"username": "admin", "password": "123456"}).json()["token"]}


def formal_n() -> int:
    return len(client.get("/api/formal", headers=H).json()["items"])


def acquire(version: str, kind: str):
    return client.post("/api/leases/acquire", headers=H,
                       json={"scope_key": f"version:{version}", "kind": kind})


def show(name: str, value) -> None:
    print(f"  {name}: {json.dumps(value, ensure_ascii=False)}")


print("F1 无租约请求正式零增")
before = formal_n()
r = client.post("/api/remap", headers=H, json={"old_key": "OLD-B2"})
show("remap status", r.status_code)
show("formal before/after", [before, formal_n()])
assert r.status_code == 409 and formal_n() == before

print("F2 持租成功可查、写入即释放")
lease = acquire("m-2026.11", "remap").json()
show("lease row", {k: lease[k] for k in ("scope_key", "holder", "kind", "lease_until", "token")})
listed = client.get("/api/leases", headers=H, params={
    "scope_key": "version:m-2026.11", "active_only": True}).json()["items"]
show("live lease verified", [(x["scope_key"], x["kind"], x["active"]) for x in listed])
r = client.post("/api/remap", headers=H,
                json={"old_key": "OLD-B2", "lease_token": lease["token"]})
show("remap status / entity", [r.status_code, r.json().get("entity_key")])
hist = client.get("/api/leases", headers=H,
                  params={"scope_key": "version:m-2026.11"}).json()["items"]
show("lease released after write", [(x["released_at"] is not None, x["active"]) for x in hist])
assert r.status_code == 200 and formal_n() == before + 1
assert hist[0]["released_at"] is not None

print("F3 两路同时抢同 scope，后到只留争用")
results: list[int] = []
barrier = threading.Barrier(6)


def worker(i: int):
    barrier.wait()
    results.append(acquire("m-2026.12", "cutover" if i % 2 == 0 else "remap").status_code)


ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
for t in ts:
    t.start()
for t in ts:
    t.join()
live = client.get("/api/leases", headers=H, params={
    "scope_key": "version:m-2026.12", "active_only": True}).json()["items"]
con = [c for c in client.get("/api/contentions", headers=H).json()["items"] if c["path"] == "lease"]
show("status codes", sorted(results))
show("live leases on scope", len(live))
show("contention audit rows", len(con))
assert results.count(200) == 1 and len(live) == 1 and len(con) == 5

print("\n夹具全通过（SQLite: %s）" % A.DB_PATH)
