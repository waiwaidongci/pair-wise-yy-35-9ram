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
- `POST /api/dose/readings`：上报个人剂量读数（dosimetrist），按`external_ref`拒绝重复上报
- `POST /api/dose/readings/{id}/correct`：退回更正，新值取代旧值，原记录保留为`superseded`
- `POST /api/dose/aggregate`：月度归集（radiation_officer），同一人员同一周期按来源汇总
- `POST /api/dose/seal`：封存周期，当前归集结果转为`confirmed`并冻结
- `GET /api/dose/summary?person_id=&period=[&version=]`：来源明细、累计量、上一周期对比与调查结论
- `GET /api/dose/readings?person_id=&period=`：全部读数（含被更正的原记录）

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。

## 月度归集规则

- 同一人员同一周期（`YYYY-MM`）按来源汇总有效读数，重复上报按来源唯一标识拒绝。
- 更正读数以新记录取代旧值，原记录保留且状态置为`superseded`，只能对最新有效读数发起更正。
- 周期封存后拒绝新增读数，但仍接受退回更正；封存后再次归集会生成新版本，已确认（`confirmed`）版本不被修改。
- 月调查水平为`2.0 mSv`，累计量达到即判定必须调查；查看结果包含来源明细、累计量、与上一周期（优先取已确认版本）的差值对比及调查结论。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
