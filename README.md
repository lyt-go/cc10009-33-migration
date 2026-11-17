# 分布式网页抓取调度平台

面向分布式网页抓取任务调度、节点协同、请求分配、反爬策略编排、数据校验入库与抓取质量大盘的采集工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集节点 | `worker` | 工作节点 | 节点编号、主机地址、并发槽位数 |
| 请求分配 | `dispatch` | 分配策略 | 策略编号、目标域名、分配算法 |
| 抓取作业 | `crawljob` | 作业定义 | 作业编号、目标站点、起始URL |
| URL过滤 | `urlfilter` | 过滤规则 | 规则编号、包含模式、排除模式 |
| DNS解析 | `dnsresolver` | 解析配置 | 配置编号、域名模式、DNS服务器 |
| 内容提取 | `contentextract` | 提取器 | 提取器编号、内容类型、正文选择器 |
| 存储策略 | `storagestrategy` | 存储方案 | 方案编号、存储类型、连接参数 |
| 渲染任务 | `rendertask` | 渲染作业 | 作业编号、目标URL、浏览器版本 |
| 采集质量 | `qualityreport` | 质量报告 | 报告编号、作业编号、总URL数 |
| 带宽控制 | `bandwidth` | 带宽策略 | 策略编号、节点标识、下载带宽上限 |
| 重试队列 | `retryqueue` | 重试条目 | 条目编号、失败URL、失败原因码 |
| 翻页管理 | `pagemarker` | 翻页规则 | 规则编号、目标站点、翻页类型 |
| 增量标记 | `incrementmark` | 增量水位 | 水位编号、目标源、增量字段名 |
| 配置同步 | `configsync` | 同步任务 | 任务编号、源节点、目标节点 |
| 采集API | `scrapeapi` | API定义 | API编号、API名称、请求路径 |
| 数据抽样 | `datasample` | 抽样方案 | 方案编号、数据源、抽样方式 |
| 采集告警 | `alertpolicy` | 告警策略 | 策略编号、监控指标、触发条件 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA

## 配置同步：分阶段迁移

配置同步原来是"一次性确认"载荷：发起同步 → 确认完成，确认即整份完成，中断后
没有"哪些段已经应用"的记录。现在把旧载荷转换成按段应用的分阶段同步结果，旧任务
和旧接口仍然可用。

### 载荷版本

| 载荷版本 | 说明 |
| --- | --- |
| `legacy-v1` | 旧版一次性确认载荷；`差异摘要` 按逗号/顿号/换行拆段，`已应用段` 记录中断时已应用的段 |
| `staged-v2` | 新载荷；登记时按 `阶段列表`（或 `差异摘要`）建立阶段计划，逐段应用/确认 |

早期记录没有 `payload_version` 字段，按 `legacy-v1` 处理。

### 数据表

| 表 | 用途 |
| --- | --- |
| `configsync` | 同步任务（原表，字段不变；新增 `payload_version`、`幂等标识`） |
| `configsync_audit` | 同步审计；旧审计原样保留，阶段动作追加记录 |
| `configsync_staged` | 分阶段同步结果的段记录（应用状态、确认状态、幂等标识） |
| `configsync_conflicts` | 待处理记录（版本回退、版本依据、映射冲突） |
| `node_config_versions` | 节点配置版本；`完整=True` 且 `状态=生效` 才允许工作终端下发 |
| `configsync_migrations` | 每次迁移的载荷摘要与文档，可回溯转换过程 |
| `work_terminal_manifests` | 工作终端清单，每个节点一条 |
| `work_terminal_manifest_logs` | 清单刷新记录 |

### 接口

| 接口 | 说明 |
| --- | --- |
| `GET /api/configsync?keyword=&status=&page=&size=` | 列表，附载荷版本与阶段进度 |
| `POST /api/configsync` | 登记任务；只给任务编号/源节点/目标节点时自动生成明确版本号 |
| `POST /api/configsync/{id}/actions` | 旧链路动作：发起同步 / 确认完成 / 标记失败 |
| `POST /api/configsync/{id}/migrate` | 旧载荷 → 分阶段同步结果；重复提交按幂等返回 |
| `GET /api/configsync/{id}/result` | 分阶段同步结果数据集（逐项复核阶段、版本依据、待处理记录） |
| `GET /api/configsync/results` | 全部分阶段同步结果数据集导出 |
| `POST /api/configsync/{id}/stages/{no}/actions` | 按阶段应用 / 确认；同一阶段重复提交按幂等处理 |
| `POST /api/configsync/{id}/replay` | 旧任务重放：只续做未应用段，节点版本不倒退 |
| `GET /api/configsync/{id}/trace` | 任务 → 分阶段结果 → 节点配置版本 → 审计 |
| `GET /api/configsync/lookup?task_no=&source_node=&version=` | 按任务编号/源节点/版本号回溯 |
| `GET /api/configsync/{id}/audit` | 同步审计结果 |
| `GET /api/configsync/{id}/conflicts` | 待处理记录 |
| `GET /api/configsync/{id}/terminal` | 工作终端读取视图（只有一个完整应用版本） |
| `GET /api/workterminal/manifests` | 工作终端清单与结果摘要 |
| `POST /api/workterminal/manifests/{node}/refresh` | 刷新清单并留一条记录 |
| `GET /api/workterminal/manifests/{node}/logs` | 清单刷新记录 |
| `GET /api/workterminal/nodes/{node}/config` | 工作终端读取入口：只暴露一个完整应用版本 |

### 迁移规则

- **中断续做**：迁移只把旧载荷拆段并保留已应用依据，不会重放已应用段；`replay`
  只续做未应用段，遇到版本回退只记录待处理，不硬推剩余段。
- **阶段幂等**：每个阶段有 `<任务幂等键>:stage-NN:应用阶段` 与 `:确认阶段` 两个幂等标识；
  重复提交返回第一次结果，不重复写节点版本、不重复写审计、不新增阶段行。
- **并发**：迁移、阶段应用、确认共用一把进程内可重入锁，并发提交只有一个请求真正
  生成结果或写版本，其余按幂等返回；同一节点同一版本只允许一条节点版本记录。
- **两组版本依据**：旧审计已确认 v3、节点只落库 v2 时，结果数据集同时列出 `旧审计`
  与 `节点版本记录` 两组依据，并保留一条待处理记录；节点版本补齐后自动解决。
- **清单只暴露一个版本**：工作终端的 `已完整应用版本` 只取节点上唯一完整的生效版本；
  更高版本区内核完整前，旧版本继续可读；新版本完整生效后，旧版本标为 `已替代`
  （记录保留，可追溯）。
- **审计兼容**：`确认完成` 重放不会新增第二条同幂等标识的审计；节点版本只升不降。

### 部署与本地开发

- 新增依赖：无。迁移逻辑只使用标准库 + 现有 FastAPI/Pydantic；测试额外需要
  `requirements-dev.txt` 里的 httpx。
- 数据库环境：当前仓库是内存仓库（`app/store.py`），表结构改由 seed 初始化；接入
  真实数据库时需按上面的表建表，`configsync_staged` / `configsync_conflicts` /
  `configsync_migrations` / `work_terminal_manifests` / `work_terminal_manifest_logs`
  为新增表，`configsync` / `node_config_versions` / `configsync_audit` 为扩展表。
- 部署顺序：先建表并回填 `node_config_versions`（节点当前生效版本）→ 部署后端
  关闭 `SYNC_STAGED_ENABLED=0` / 保持 `SYNC_LEGACY_CONFIRM_ENABLED=1` → 逐个任务
  执行 `/migrate` 生成分阶段结果 → 打开 `SYNC_STAGED_ENABLED=1` → 等旧任务重放完毕
  后再关闭 `SYNC_LEGACY_CONFIRM_ENABLED`。前端只读接口，可最后发布。
- 本地开发：

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
SYNC_STAGED_ENABLED=1 ./run.sh
```

运行迁移行为测试（测试需要 httpx，已经放在 `requirements-dev.txt`，不影响运行期依赖）：

```bash
cd backend
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -v
```

或在仓库根目录执行 `make test`。
