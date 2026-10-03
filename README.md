# 化工装置变更与工艺安全管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8310`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8310
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `corridor`：管廊（分区），持有公共容量 `capacity`。
- `unit`：装置，运行时挂在管廊下（`corridor_id`，可设 `unit_occupancy`）；停车/冻结时占公共容量。
- `change`：变更申请，经装置归属到管廊，按风险等级占容量（low=1、medium=2、high=3、critical=4）。
- `action_item`：风险控制行动项，经变更归属到管廊，未复验前占 1 份容量。

## 占用账与排队

所有占用写入追加式台账 `occupancy_ledger`（`active`/`waiting`/`released`/`cancelled`），每次状态变化在一个
`BEGIN IMMEDIATE` 事务内完成核对、记账和重排：

- 变更 `implement` 前核对管廊余量：余量不足（或已有排队）进入 `queued`，不占实际容量，只留占位行。
- 排队严格 FIFO，队头不放行则后续一律等待（队头阻塞）。
- 装置 `startup`/`unfreeze`、变更 `rollback`/`close`/`commission`、行动项 `verify` 后，旧占用标记失效并自动重排，
  能容纳的排队变更自动提升为 `implemented`（审计动作为 `promote`）。
- 装置 `shutdown`/`freeze` 立即占用公共容量（可能超卖，`available` 为负）。
- 安全员（`safety` 角色）执行 `freeze` 时，该装置下所有未实施变更的排队占位失效，变更退回 `approved` 需重新申请；
  操作员/管理员的普通冻结不影响排队。已实施占用不受冻结影响。
- 同管廊并发实施申请由数据库写事务串行化，只放行一份，其余排队。
- 创建和动作都支持 `Idempotency-Key` 请求头：写入失败后安全重试，不会重复占用（排队后被自动提升的请求，
  重试返回提升后的最新实体）。
- 越权动作返回 `403 PermissionDenied`。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON，支持`Idempotency-Key`。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`，
  支持`Idempotency-Key`。实施余量不足时返回状态为`queued`的对象（HTTP 200）。
- `GET /api/corridors/<id>/occupancy`：管廊占用账，返回`capacity/used/available`及`active`、`waiting`明细。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

风险分级和投产规则用于流程演示，不替代HAZOP、LOPA、法定许可和现场安全审查。
