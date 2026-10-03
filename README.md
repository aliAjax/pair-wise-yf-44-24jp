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

- `unit`：装置运行状态；`change`：变更申请；`action_item`：风险控制行动项。
- `occupancy`：占用账，把装置停车/冻结、变更实施、行动项复验挂到同一份管廊容量上。

## 管廊分区与占用账

园区按管廊分区（装置的 `zone` 字段，缺省回退到 `location`），每区有公共容量：

- 装置 `shutdown`/`freeze` 强制占用固定当量（安全动作，即使余量不足也先占用）；`startup`/`unfreeze` 释放并重排队列。
- 变更 `implement` 时按风险等级占容量（low=1 / medium=2 / high=3 / critical=4）：余量足够则占用并实施，不足则变更落为 `queued` 排队。
- 行动项创建即占用，`verify` 后释放并重排；`reopen` 重新占用。
- 装置恢复或行动项复验后触发重排，FIFO 激活排队中的变更并自动实施。
- 安全员可对变更 `freeze`/`unfreeze`：冻结仅使未实施的排队占用失效，变更进入 `frozen`。

并发保证：核对余量→占用在 `BEGIN IMMEDIATE` 写事务内串行完成；占用表上的部分唯一索引 `WHERE status IN ('active','queued')` 保证同区同一对象只放一份；配合 `Idempotency-Key` 重试不重复占用。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`，支持`Idempotency-Key`请求头。
- `GET /api/occupancies`：占用账，可用`?zone=&status=`过滤。
- `GET /api/zones`：各管廊分区容量、占用与余量。
- `GET /api/zones/<zone>`：单分区容量、占用、活动占用与排队队列。
- `POST /api/zones/<zone>/capacity`：设置分区容量（仅管理员），请求体`{"capacity":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制（冻结/解冻变更仅管理员与安全员）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

风险分级和投产规则用于流程演示，不替代HAZOP、LOPA、法定许可和现场安全审查。
