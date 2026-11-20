# 实体键桥

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账

## 正确性口径（全局唯一，择甲路径）

**甲路径：先租后写（lease-before-write）**，全仓只此一种，禁止与「租约与写入同提交、失败再回滚」的乙路径混用。

| 约束 | 落地 |
|------|------|
| 租约物化可核证 | `leases` 表：`scope_key` + `holder` + `lease_until` + `kind`（cutover/remap），`token` 一次性兑付 |
| 禁无租约改生效窗 | `/api/cutover` 无 token → 409 整笔拒绝，`map_versions` 零改动 |
| 禁无租约写正式 | `/api/remap` 无 token → 409 整笔拒绝，`formal_entities` 零增 |
| 禁租约失败后正式增量 | 租约校验、写入、释放同一 `BEGIN IMMEDIATE` 事务 |
| 禁两路同 scope 双写 | 同 scope 存活租约唯一（部分唯一索引）；后到只写 `contention_audit`，不铸新 token |
| 绕过应用层 | SQLite 触发器 `trg_map_versions_promote_lease` / `trg_map_versions_demote_lease` / `trg_formal_entities_lease` 二次核验租约 |

**scope 命名**：`version:<映射版本>`。切版与重映共用同一命名空间，故两路不可能同时对同一版本持租。

**只读核证接口**：`GET /api/leases?scope_key=&active_only=`（租约账目）、`GET /api/contentions`（争用审计）。两者均不改库。

**事务写法要点**：`BEGIN IMMEDIATE` 下先校验租约→写正式/改窗→释放租约；任何校验失败只写争用审计并提交，绝不留下部分写入。

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 实体键桥 |
| 目录 | `07-keyremap` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10918 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

一对多/多对一、生效窗、旧键只读、重映重放

## 避开

别做成 Feed 冲突归并；正式区不是 staging 换皮

## 互斥切口候选

- 冲突拒收整批 vs 进冲突区后重放
- 废弃旧键只读保留 vs 重写正式键
- 生效窗原子切换 vs 双窗按事件时裁

## 启动（一键）

`powershell
cd projects/07-keyremap
docker compose up -d --build
`

浏览器：http://127.0.0.1:10918/  
健康检查：GET http://127.0.0.1:10918/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10604
python -m pytest -q
python fixtures/lease_race_fixture.py   # 独立夹具：无租约零增 / 持租可查 / 抢租争用
`
