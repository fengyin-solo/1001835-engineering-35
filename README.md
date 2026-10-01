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

## 公众诉求发布前检查

发布前不再人工逐项核对，统一跑一条流程（步骤只以
`release-check/manifest.json` 这一处清单为准，新增/调整检查只改这个文件）：

```bash
make preflight          # 断点续跑：从上次失败或输入发生变化的步骤继续
make preflight-fresh    # 清掉全部中间产物后从头跑
make preflight-list     # 查看清单中的步骤
```

流程依次完成：核对 `backend/requirements.txt` 与虚拟环境中的依赖版本、按采集顺序
把诉求台账回填成示例数据、核对 compose 端口映射与 Dockerfile `EXPOSE`、核对环境变量
在 compose 与 `.env.example` 中都已声明、启动后端核对诉求列表/导出接口的条数与台账一致
（含采集顺序与各状态筛选口径）。

- 哪一步没过会直接打印步骤名与具体原因，详细日志在 `.release-check/steps/<步骤>.log`。
- 修好后重新 `make preflight` 即可，已通过且输入未变的步骤自动跳过；
  中间产物全部隔离在 `.release-check/`（已 gitignore），不会污染下一次运行。
- 诉求台账是 `backend/data/complaints.ledger.json`，条数核对只以这份台账为准。

