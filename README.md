# 城市地下管网巡检养护平台

面向城市给排水与燃气管网的管段建档、检查井阀门、巡查巡检、内窥检测、缺陷修复与压力流量监测的一体化养护后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
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

## 公众诉求发布前检查

发布前不用再人工挨个确认依赖版本、准备示例数据、核对端口与环境变量，统一跑一条流水线：

```bash
make preflight            # 一次跑完所有检查
make preflight-reset      # 丢弃上次进度与中间产物，从头再跑
make preflight-status     # 只看各步骤当前通过情况
python3 release-check/preflight.py --from-step health   # 从指定步骤开始
```

检查步骤（顺序固定，任一不过立即停下并打印步骤名与具体原因）：

| 步骤 | 检查内容 |
| --- | --- |
| `deps` | 重建干净虚拟环境，按清单渲染 `requirements.txt`，逐个核对依赖版本与 Python 版本 |
| `seed` | 存量诉求台账按「采集时间」升序回填成示例数据，并落盘本次快照 |
| `config` | 逐个核对容器端口（compose / Dockerfile / run.sh / vite）与环境变量声明 |
| `health` | 拉起后端并做健康探针（已在运行则复用，不重复起） |
| `complaints` | 分页拉全量诉求，核对接口条数、`total`、采集顺序、每条字段与本次回填快照一致，并核对导出接口 |

要点：

- **唯一清单**：期望值只维护在 `release-check/manifest.toml`（依赖版本、端口、环境变量、诉求台账），
  `requirements.txt` 与诉求示例数据都由它生成，不要在别处另立数字。
- **断点续跑**：通过状态记在 `.release/work/state.json`，修好失败原因后重跑会自动从未通过的步骤继续，
  已通过的步骤跳过；每步执行前先清理自己上一次的中间产物（虚拟环境、生成文件、快照、服务进程），旧产物不影响本次。
- **存量诉求**：台账在清单的 `complaints.records` 里，录入顺序无关，一律按「采集时间」升序回填；
  接口给出的条数与字段必须和台账一致，由 `complaints` 步骤强制保证。


## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 管段档案 | `pipe` | 管段 | 管段编号、管道类别、起点井号 |
| 检查井 | `manhole` | 检查井 | 井编号、所在道路、井盖类别 |
| 阀门井室 | `valve` | 阀门 | 阀门编号、阀门类别、所在管段 |
| 泵站设施 | `pumpstation` | 泵站 | 泵站编号、泵站名称、服务区域 |
| 巡查任务 | `patrol` | 巡查单 | 巡查单号、巡查路线、巡查人员 |
| 缺陷登记 | `defect` | 缺陷记录 | 缺陷编号、所在管段、缺陷类别 |
| 内窥检测 | `cctv` | 检测报告 | 检测编号、检测管段、检测设备 |
| 修复施工 | `repair` | 修复单 | 修复单号、关联缺陷、修复方式 |
| 压力监测 | `pressure` | 压力记录 | 监测编号、监测点位、监测时段 |
| 流量监测 | `flow` | 流量记录 | 监测编号、监测断面、监测时段 |
| 泄漏排查 | `leak` | 排查记录 | 排查编号、排查区域、排查方式 |
| 清淤疏浚 | `dredge` | 清淤单 | 清淤单号、清淤管段、淤积厚度 |
| 养护材料 | `material` | 养护材料 | 材料编号、材料名称、规格型号 |
| 养护机械 | `equip` | 养护机械 | 机械编号、机械名称、机械型号 |
| 占道许可 | `traffic` | 占道许可 | 许可编号、申请单位、占道位置 |
| 公众诉求 | `complaint` | 诉求记录 | 诉求编号、诉求来源、诉求内容 |
| 养护资金 | `fund` | 资金记录 | 资金编号、费用类别、项目名称 |
| 管网档案 | `archive` | 档案记录 | 档案编号、关联管段、档案类别 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
