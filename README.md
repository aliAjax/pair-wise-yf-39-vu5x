# 野生动物疫病监测与离线同步

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8305`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/reconcile.py`：实验室回执对账、去重、挂起/拒绝、重试与离线合并。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8305
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `observation`：现场观察；`sample`：样本与实验室结果；`cluster`：异常聚集事件。
- `lab_order`：送检单，记录某批次（`batch_no`）送往某实验室（`lab_id`）的样本编号清单，是中心台账的对账基准。
- `receipt`：实验室回执，携带`lab_id`、`batch_no`和逐样本`items`，入库后按台账逐条对账。

## 回执对账

- `POST /api/receipts`：接收回执并立即对账；请求体`{"lab_id","batch_no","items":[{"sample_code","result","result_at"}]}`，支持`Idempotency-Key`请求头。带`"mode":"offline"`时只落本地`outbox`，不触碰样本。
- `POST /api/receipts/<id>/retry`：重试该回执的待重试条目。
- `POST /api/sync`：联网后把`outbox`回执合并回中心台账，并重试所有`pending_retry`回执；按批次和样本去重，重复合并不会重复记录。
- `GET /api/receipt-items`：按`receipt_id`、`batch_no`、`sample_code`、`state`查询对账台账。

对账规则：

- 按`(batch_no, sample_code)`去重，重发的回执记为`duplicate`，不会把一支样本的结果记两遍。
- 批次无送检单、样本编号不在送检单内或中心台账查不到样本：条目标记`suspended`挂起，不改样本状态。
- 回执实验室与送检单实验室不一致（越权）：整张回执`rejected`；`lab`角色只能提交本实验室的回执。
- 条目入库失败（如版本冲突）：标记`pending_retry`留待重试，回执状态为`pending_retry`。
- 样本结论被回执或复检改变时，引用它的聚集事件先置为`invalidated`，再按区域内阳性样本重算成员：仍满足时空窗口（14天/10公里、≥3例）则回到`confirmed`并更新成员，否则保持失效。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

离线同步使用批次和幂等键演示，不包含真实野外通信协议、地图底图或完整空间索引。
