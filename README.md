# 银龄服务与权益分析台

Silverwatch 是面向地区养老服务管理与消费者权益协作人员的离线分析服务。它把服务机构和供给、地区需求估计、实际触达、群众线索、核实决定、整改复核和分析公告保存在同一条可追溯的业务链中。项目中的示例记录均为虚构数据。

## 运行

项目要求 Python 3.11 或更新版本。依赖按 requirements.txt 精确锁定；数据保存在本机 SQLite 文件中。启动 HTTP 服务：

    uvicorn silverwatch.api:create_app --factory --host 127.0.0.1 --port 8000

服务启动后可在 /docs 查看接口契约。SILVERWATCH_DB 环境变量可以指定 SQLite 文件位置；未设置时使用当前目录下的 silverwatch.sqlite3。

## 数据流程

机构目录、需求估计、触达记录和线索可通过单条接口或 CSV、NDJSON 批次导入。批次预检会指出源行及字段错误；正式导入在一个 SQLite 写事务内提交，任何一条不符合契约都会使整批回滚。相同来源修订可以安全重放，已存在修订不能换内容覆盖。

案件决定区分待核实、核实成立、未成立和意见不一致。成立决定需引用已登记且在决定时点之前存在的核实材料；调查与合规决定使用不同岗位和人员。整改材料由责任人以外的人员核验，只有完成核实的案件整改才能关闭。

分析同时使用事件序号截点和事实观察时间。触达分母、未回应人数、来源完整性、已核实线索和供给估计分别显示，未核实线索不会计入成立案件；缺少来源覆盖、样本不足或关联对账未完成时会标明限制。地区比较和趋势回放固定共同政策与截点，保留待核实线索和分母变化。

公告需服务规划与消费者保护岗位分别复核。签发后保留公告版本、事实截点与摘要；公开 JSON、CSV 和公告读取接口会隐藏源批次及服务方标识，并按小样本阈值抑制细分数字。内部审计接口可核验事件摘要、分析输入截点和公告内容。

## 政策版本与适用口径

分析门槛（最小触达人数、供给覆盖率、核实率上限、严重未结案件与整改复核要求等）可以作为**带生效区间的版本**登记和选择。政策版本保存在独立的 `policy_version` 表中，与事实事件账本分离：登记或切换政策不会推进事件截点，也不会改写任何已保存分析或已签发公告。

登记政策版本（`effective_to` 可省略；省略时该版本的区间到下一版本 `effective_from` 为止，区间按半开 `[effective_from, effective_to)` 处理；日期可只写到日，按 UTC 零点解释）：

    POST /policies
    {"policy_id":"eligibility","version":1,"effective_from":"2026-01-01",
     "max_substantiated_rate_bp":60, ...}
    POST /policies
    {"policy_id":"eligibility","version":2,"effective_from":"2026-11-01",
     "max_substantiated_rate_bp":40, ...}
    GET  /policies/eligibility/effective?at=2026-10-15   # 生效前 → v1 旧口径
    GET  /policies/eligibility/effective?at=2026-11-15   # 生效后 → v2 新规则

分析、地区比较与趋势请求除继续支持内联 `policy` 外，也支持 `policy_id`（可加 `policy_version` 与 `policy_mode`）：

* `policy_mode="effective"`（给出 `policy_id` 时的默认值）：按每个评估时点选择当时生效的版本。趋势跨年度新旧门槛时，生效前各期用旧口径、生效后各期用新规则，`policy_schedule`/`policy_switches` 与每份分析文档的 `policy_basis` 会说明该期适用哪一版及原因。
* `policy_mode="fixed"`（或显式给出 `policy_version`、`uniform_policy=true`）：所有期间/地区统一使用同一版政策重算历史，得到可横向比较的结果。`POST /analyses/{analysis_id}/recompute` 可用指定版本对已存分析的相同数据截点试算，默认不另存、不改写已签结果。

每份分析文档都含 `policy_basis`（模式、版本、生效区间、评估时点与中文适用口径说明）以及同义的 `applied_policy`/`policy_context` 字段；公告整体冻结该文档与摘要，因此事后登记新版本不会改变旧公告，`/audit/...` 完整性校验依旧通过。

当**事实更正**（来源修订）与**政策生效**同时改变某地区资格时，`POST /analysis-attribution` 用 2×2 反事实重算分解来源：A=旧数据+旧政策、B=新数据+旧政策（A→B 只反映数据）、C=新数据+新政策（B→C 只反映规则），并用 D=旧数据+新政策识别反向规则效应。每个地区给出 `source`（`data`/`policy`/`both`/`none`）、`data_change`/`policy_change`、指标变化、门槛变化与触发的缺口；也可用 `GET /analyses/{id}/attribution?left=&right=` 或 `/bulletins/{id}/attribution?left=&right=` 比较已存修订/公告。

## 本地验收

    python -m unittest discover -s tests -v
    python -m compileall -q silverwatch tests
    python -m silverwatch --fixture fixtures/silverwatch-demo.json

验收场景会在临时目录创建数据库，登记虚构的服务与触达事实，完成案件材料核验、双岗决定、整改复核、供需分析和公告签发。所有测试与启动流程均使用本地内容，不依赖第三方 API 或在线数据。
