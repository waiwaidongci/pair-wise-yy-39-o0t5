# 大坝巡检、缺陷与应急管理（版本台账）

安排巡检，记录渗流、位移、裂缝等缺陷并跟踪修复、复检和应急预案。缺陷、巡检记录、应急签发三者接入**版本台账**，解决汛期同一缺陷反复补测、后补读数带偏已签发结论、两人同时上报互相覆盖的问题。

## 版本台账规则

- 巡检上报必须携带 **请求号 `request_id`** 与 **基准版本 `base_version`**。
  - **同号重复提交**：沿用第一次结果，不重复生效、不改读数（幂等）。
  - **两人同时上报**：基准版本与当前版本一致者先到成立，确认读数并推进版本；后到者基准过期，内容**留作冲突**，不覆盖已确认读数。冲突方刷新到新版本后用新请求号可重新上报。
- **新读数成立或控制阈值变化**：所有未完成（待复核/已复核未签发）的应急签发单**立即失效**，按新依据**重算优先级与期限**并生成新签发单；失效来源（如 `new_reading:REQ-2`、`threshold_change:10.0->3.0`）逐单留痕。
- **已签发结论保留当时依据**：签发时冻结基准版本、优先级、期限和结论，后续读数变化只生成新的待签发单，不改写历史。
- **角色分工**：巡检员 `inspector` 上报；坝工程师 `dam_engineer` 复核签发单、变更控制阈值、补核旧记录；应急负责人 `emergency_manager` 签发；其他角色（`viewer` 等）只能查看。
- **旧记录**缺请求号或基准版本时自动升级为**待补核 `pending_verification`**，由坝工程师带当前基准版本补核后方成立。
- 台账页面展示当前版本、签发失效来源与冲突内容。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、签发/上报结论常量。
- `src/repository.py`：SQLite建表、版本台账（submissions / issuances）、事务内失效联动和审计链。
- `src/service.py`：权限检查、幂等上报、补核、阈值联动、签发编排和台账装配。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：版本台账演示页（身份/角色切换、上报、冲突、补核、复核、签发）。
- `tests/`：完整流程、规则、失败场景与版本台账测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8316
```

默认端口为`8316`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `GET /health` | 全部 | 健康检查 |
| `GET /api/items` / `GET /api/items/{id}` | 全部可查看角色 | 缺陷列表/详情 |
| `POST /api/items` | inspector | 建缺陷 |
| `POST /api/items/{id}/records` | inspector（带请求号）；旧记录 dam_engineer 亦可登记 | 上报：带 `request_id`+`base_version`+`reading` 走版本台账；两者都缺则转待补核 |
| `POST /api/items/{id}/records/verify` | dam_engineer | 补核旧记录 `{record_id,base_version,reading}` |
| `GET /api/items/{id}/ledger` | 全部可查看角色 | 版本台账：当前版本、记录、上报、冲突、待补核、签发链与失效来源 |
| `POST /api/items/{id}/issuances/review` | dam_engineer | 复核 `{issuance_id,base_version}` |
| `POST /api/items/{id}/issuances/issue` | emergency_manager | 签发 `{issuance_id,base_version,conclusion}` |
| `POST /api/items/{id}/threshold` | dam_engineer | 变更控制阈值 `{base_version,threshold}`，联动失效与重算 |
| `POST /api/items/{id}/transition` | 按状态分工 | 状态流转，必须提交 `expected_version` |
| `GET /api/audit` | emergency_manager, viewer | 审计链 |

上报响应：

- 成立：`{"resolution":"confirmed", ...}`，同号重放额外返回 `"replayed":true` 与第一次结果。
- 冲突：`{"resolution":"conflict", ...}`，内容进入台账 `conflicts`。
- 旧记录：`{"resolution":"pending_verification","record":{...}}`。

异常值比控制阈值越高，缺陷优先级越高；应急处置缺陷必须完成复检并记录证据后才能关闭。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
