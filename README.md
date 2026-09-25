# 职业辐射剂量与异常事件

合并监测读数，比较历史剂量并管理超限调查、医学随访与报告期限。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8312
```

默认端口为`8312`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。

## 月度归集接口

同一人员同一周期按监测系统来源（source）汇总；重复上报（周期+来源+记录编号相同）拒绝，更正读数以新行取代旧值并保留原记录（status=superseded）。周期封存（seal）后周期内草稿全部转为已确认；封存后再归集自动生成新版本草稿，已确认结果固定不变。查看视图返回来源明细、全部读数、累计量、上一周期对比和是否必须调查。

- `POST /api/dose/persons`：登记人员 `{code,name}`（dosimetrist / radiation_officer）
- `GET /api/dose/persons`
- `POST /api/dose/readings`：上报读数 `{person_id,period(YYYY-MM),source,external_ref,value}`（dosimetrist）；封存周期上报返回409
- `POST /api/dose/readings/{id}/corrections`：更正读数 `{value,reason}`，旧记录保留
- `GET /api/dose/persons/{id}/months/{period}/readings`
- `POST /api/dose/collections`：归集 `{person_id,period}`（dosimetrist / radiation_officer）；开放期更新草稿v1，封存后生成v2、v3…
- `POST /api/dose/periods/seal`：封存周期 `{period}`（radiation_officer），封存即确认全部草稿
- `POST /api/dose/summaries/confirm`：单独确认某人某周期草稿（radiation_officer）
- `GET /api/dose/persons/{id}/months/{period}/view`：归集视图（来源明细、累计量、上一周期对比、investigation_required）
- `GET /api/dose/periods`、`GET /api/dose/summaries?period=YYYY-MM`

调查判定：月累计达到2.0mSv，或较上一周期已确认值增长达到3倍，即 `investigation_required=true` 并给出原因。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
