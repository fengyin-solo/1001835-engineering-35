#!/usr/bin/env python3
"""公众诉求发布前检查流水线。

把发布前要人工挨个儿确认的环节串成一条流水线：

  deps        核对后端依赖版本（以 manifest 为准，渲染 requirements.txt）
  seed        存量诉求台账按采集顺序回填成示例数据
  config      逐个核对容器端口与环境变量声明
  health      拉起后端并做健康检查
  complaints  接口给出的诉求条数/顺序/字段与台账列表完全一致

特性：
- 任一不过立即停在该步，打印步骤名与具体原因；
- 通过状态记在 work_dir/state.json，修好后重跑自动从失败步继续；
- 每个步骤执行前先清掉自己上一次留下的中间产物，旧产物不影响本次；
- 期望值只来自 release-check/manifest.toml 这一处清单。

用法：
  python3 release-check/preflight.py            从上次失败处继续
  python3 release-check/preflight.py --reset    忽略进度，从头跑
  python3 release-check/preflight.py --from-step health
  python3 release-check/preflight.py --status   只看各步状态
"""
from __future__ import annotations

import argparse
import json
import os
import pprint
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import tomllib

ROOT = Path(__file__).resolve().parent.parent

# ---------- 终端输出 / 步骤失败 ----------


class StepFailed(Exception):
    """某一步未通过；message 就是给人看的具体原因。"""


def green(text: str) -> str:
    return f"\033[32m{text}\033[0m"


def red(text: str) -> str:
    return f"\033[31m{text}\033[0m"


def yellow(text: str) -> str:
    return f"\033[33m{text}\033[0m"


# ---------- 流水线骨架 ----------


class Pipeline:
    def __init__(self, manifest: dict[str, Any]) -> None:
        self.m = manifest
        self.work_dir = ROOT / manifest["pipeline"]["work_dir"]
        self.state_path = self.work_dir / "state.json"
        self.log_path = self.work_dir / "run.log"
        self.steps: list[tuple[str, str, Callable[["Pipeline"], None]]] = []
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._log = self.log_path.open("a", encoding="utf-8")
        self.server: subprocess.Popen[bytes] | None = None
        # 是否由本流程拉起（续跑时可能复用已在运行的服务，那种不能替用户杀掉）
        self.owns_server = False

    def step(self, key: str, title: str) -> Callable[[Callable[["Pipeline"], None]], Callable[["Pipeline"], None]]:
        def register(fn: Callable[["Pipeline"], None]) -> Callable[["Pipeline"], None]:
            self.steps.append((key, title, fn))
            return fn

        return register

    def say(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        print(line)
        self._log.write(line + "\n")
        self._log.flush()

    # ---- 状态（断点续跑） ----

    def load_state(self) -> dict[str, str]:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                # 状态文件本身是中间产物：损坏就当成没跑过，不影响本次
                return {}
        return {}

    def save_state(self, state: dict[str, str]) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def reset_progress(self) -> None:
        """丢掉上次全部进度与日志：状态文件删除，运行日志截断重开。"""
        self.state_path.unlink(missing_ok=True)
        self._log.close()
        self.log_path.unlink(missing_ok=True)
        self._log = self.log_path.open("a", encoding="utf-8")

    # ---- 通用辅助 ----

    def run_cmd(self, cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._log.write(f"$ {' '.join(cmd)}\n{proc.stdout}")
        self._log.flush()
        return proc

    def write_atomic(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

    def cleanup(self) -> None:
        """流程结束（含失败）时收尾：只回收自己拉起的服务。

        复用的、用户手动启动的服务不动；占用端口的判断在 health 步骤里做。
        """
        if self.server is not None and self.owns_server and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server.kill()

    def close(self) -> None:
        self.cleanup()
        self._log.close()


# ---------- 版本号比较（不引第三方依赖） ----------


def parse_version(text: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", text)
    return tuple(int(p) for p in parts) if parts else (0,)


def version_satisfies(installed: str, spec: str) -> bool:
    """支持 >= / <= / > / < / == / ~= 与逗号分隔的复合约束。"""
    ver = parse_version(installed)
    for clause in spec.split(","):
        clause = clause.strip()
        m = re.match(r"^(>=|<=|==|~=|>|<)?\s*(.+)$", clause)
        if not m:
            return False
        op, want = m.group(1) or ">=", parse_version(m.group(2))
        if op == ">=" and not ver >= want:
            return False
        if op == ">" and not ver > want:
            return False
        if op == "<=" and not ver <= want:
            return False
        if op == "<" and not ver < want:
            return False
        if op in ("==", "~=") and ver[: len(want)] != want:
            return False
    return True


# ---------- 各检查步骤 ----------


def register_steps(p: Pipeline) -> None:
    m = p.m

    @p.step("deps", "后端依赖版本核对")
    def step_deps(p: Pipeline) -> None:
        b = m["backend"]
        backend_dir = ROOT / b["dir"]
        venv_dir = backend_dir / ".venv"
        pip_flags = ["--disable-pip-version-check"]

        # 中间产物：本次自建的 venv。先清掉旧的/坏链接的，避免上一次环境干扰
        if venv_dir.exists():
            p.say("清理上次的虚拟环境 .venv")
            shutil.rmtree(venv_dir)

        p.say("创建虚拟环境")
        create = p.run_cmd([sys.executable, "-m", "virtualenv", str(venv_dir), "-q"])
        if create.returncode != 0:
            # 部分系统装的是 python3-venv 而没有 virtualenv，回退到标准库 venv
            create = p.run_cmd([sys.executable, "-m", "venv", str(venv_dir)])
        if create.returncode != 0:
            raise StepFailed(
                "创建虚拟环境失败（需要 virtualenv 或 python3-venv）。输出：\n" + create.stdout[-800:]
            )

        # 唯一事实来源：requirements.txt 由清单渲染
        deps = b["dependencies"]
        rendered = "".join(f"{d['name']}{d.get('extra', '')}{d['spec']}\n" for d in deps)
        req_path = ROOT / b["requirements"]
        if req_path.exists() and req_path.read_text(encoding="utf-8") != rendered:
            p.say(f"按清单覆盖 {b['requirements']}（旧内容与清单不一致）")
        p.write_atomic(req_path, rendered)

        py = str(venv_dir / "bin" / "python")
        p.say("安装/核对 requirements.txt")
        install = p.run_cmd(
            [py, "-m", "pip", "install", "-q", *pip_flags, "-r", str(req_path)]
        )
        if install.returncode != 0:
            raise StepFailed("依赖安装失败，pip 输出：\n" + install.stdout[-1200:])

        # Python 版本
        probe = p.run_cmd([py, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"])
        if probe.returncode != 0:
            raise StepFailed("无法在虚拟环境中执行 python：\n" + probe.stdout[-400:])
        py_ver = probe.stdout.strip()
        if not version_satisfies(py_ver, ">=" + b["python_min"]):
            raise StepFailed(f"Python 版本 {py_ver} 低于清单要求 >={b['python_min']}")

        # 逐个按清单核对安装版本
        for dep in deps:
            dist = dep.get("dist", dep["name"])
            got = p.run_cmd(
                [
                    py,
                    "-c",
                    "from importlib.metadata import version; print(version(%r))" % dist,
                ]
            )
            if got.returncode != 0:
                raise StepFailed(f"依赖 {dist} 未安装成功，元数据查询失败：\n{got.stdout[-400:]}")
            installed = got.stdout.strip()
            if not version_satisfies(installed, dep["spec"]):
                raise StepFailed(
                    f"依赖 {dist} 已装版本 {installed} 不满足清单约束 {dep['spec']}"
                )
            p.say(f"  {dist}{dep['spec']} -> {installed}")

    @p.step("seed", "存量诉求按采集顺序回填示例数据")
    def step_seed(p: Pipeline) -> None:
        c = m["complaints"]
        records = list(c["records"])
        if not records:
            raise StepFailed("清单 complaints.records 为空，没有可回填的存量诉求")

        # 唯一性与字段完整性
        seen_ids: set[str] = set()
        for idx, rec in enumerate(records, 1):
            for field in ["诉求编号", "采集时间"]:
                if not str(rec.get(field, "")).strip():
                    raise StepFailed(f"台账第 {idx} 条缺少必填字段「{field}」")
            num = rec["诉求编号"]
            if num in seen_ids:
                raise StepFailed(f"台账诉求编号重复：{num}")
            seen_ids.add(num)
            for field in c["required_fields"]:
                if field not in rec:
                    raise StepFailed(f"台账 {num} 缺少字段「{field}」，请在清单中补齐")

        # 回填顺序：采集时间升序（早采集的排前面），时间相同按编号
        ordered = sorted(records, key=lambda r: (r["采集时间"], r["诉求编号"]))
        status_flow = c["status_flow"]
        pending_last = status_flow[-1]

        rows: list[dict[str, Any]] = []
        for i, rec in enumerate(ordered, 1):
            if rec["状态"] not in status_flow:
                raise StepFailed(
                    f"台账 {rec['诉求编号']} 的状态「{rec['状态']}」不在允许序列 {status_flow} 内"
                )
            row: dict[str, Any] = {"id": i, "status": rec["状态"]}
            row["pending"] = rec["状态"] != pending_last
            row["abnormal"] = False
            for field in c["required_fields"]:
                row[field] = rec[field]
            # 台账列表展示列「诉求状态」与内部 status 同源，保证前端这一列不为空、
            # 且与 status 筛选口径一致
            row["诉求状态"] = rec["状态"]
            row["采集时间"] = rec["采集时间"]
            rows.append(row)

        # 中间产物：生成的示例数据文件。每次全量重写（原子替换），
        # 因此旧的多余记录/旧顺序不会残留。
        # 注意这是 Python 模块：用 pprint 而不是 json，布尔要渲染成 True/False
        payload = pprint.pformat(rows, indent=2, width=100, sort_dicts=False)
        header = (
            "# 此文件由 release-check/preflight.py 按 release-check/manifest.toml 自动生成，\n"
            "# 请勿手工编辑；存量诉求的维护入口是清单里的 complaints.records。\n"
        )
        p.write_atomic(ROOT / c["generated_seed"], header + "COMPLAINT_SEED_ROWS = " + payload + "\n")
        # 本次真正回填落盘的快照：complaints 步骤以它为基准核对接口，
        # 从而能抓出"接口没吃到本次回填（旧种子/没重启/路由串数据）"
        p.write_atomic(
            p.work_dir / "complaint_seed_snapshot.json",
            json.dumps(rows, ensure_ascii=False, indent=2),
        )
        p.say(f"已按采集时间回填 {len(rows)} 条诉求示例数据（{ordered[0]['诉求编号']} … {ordered[-1]['诉求编号']}）")

    @p.step("config", "容器端口与环境变量核对")
    def step_config(p: Pipeline) -> None:
        errors: list[str] = []

        def read(rel: str) -> str:
            path = ROOT / rel
            if not path.exists():
                errors.append(f"清单要求核对的文件 {rel} 不存在")
                return ""
            return path.read_text(encoding="utf-8")

        # 端口：每个声明文件里都必须出现且只应出现该宿主端口映射
        for item in m["ports"]:
            port = int(item["port"])
            for rel in item["sources"]:
                text = read(rel)
                if not text:
                    continue
                patterns = [
                    rf'"?{port}"?\s*:\s*"?{port}"?\b',  # docker-compose "8000:8000"
                    rf"--port\s+{port}\b",               # uvicorn --port
                    rf"EXPOSE\s+{port}\b",
                    rf"port\s*:\s*{port}\b",             # vite config
                ]
                if not any(re.search(pat, text) for pat in patterns):
                    errors.append(
                        f"[{item['service']} 端口] {rel} 未按清单声明端口 {port}"
                    )

        # 环境变量：变量名（和给出的取值）必须出现在对应文件
        for item in m["env_vars"]:
            name = item["name"]
            for rel in item["sources"]:
                text = read(rel)
                if not text:
                    continue
                if name not in text:
                    errors.append(f"[环境变量] {rel} 缺少清单要求的 {name}")
                    continue
                if "value" in item:
                    want = item["value"]
                    # 匹配 APP_ENV=local 或 APP_ENV: local
                    pat = rf"{re.escape(name)}\s*[:=]\s*[\"']?{re.escape(want)}[\"']?"
                    if not re.search(pat, text):
                        errors.append(
                            f"[环境变量] {rel} 中 {name} 的取值与清单不一致（应为 {want}）"
                        )

        # compose 两个服务的端口也要与清单一致（services 名以 ports/service 为准）
        if errors:
            raise StepFailed("端口/环境变量核对未通过：\n  - " + "\n  - ".join(errors))

    @p.step("health", "后端启动与健康检查")
    def step_health(p: Pipeline) -> None:
        b = m["backend"]
        port = int(b["port"])
        url = f"http://{b['host']}:{port}{b['health_path']}"

        # 续跑场景：服务可能已经在运行，活着就直接复用，不重复拉起
        if health_ok(url):
            p.say(f"后端已在运行，健康检查通过：{url}")
            p.server = None
            p.owns_server = False
            return

        py = str(ROOT / b["dir"] / ".venv" / "bin" / "python")
        if not Path(py).exists():
            raise StepFailed("后端虚拟环境不存在，请先让 deps 步骤通过")

        # 端口被占但健康检查不过：多半是残留/异常进程，不替用户杀，给出明确处置
        if sock_busy(port):
            raise StepFailed(
                f"端口 {port} 已被占用但 {b['health_path']} 无响应，"
                f"可能是上次残留的异常进程；请先停止占用进程后重跑本步"
            )

        p.say(f"启动后端：{b['module']} @ {b['host']}:{port}")
        log_fp = (p.work_dir / "backend.log").open("wb")
        p.server = subprocess.Popen(
            [
                py,
                "-m",
                "uvicorn",
                b["module"],
                "--host",
                b["host"],
                "--port",
                str(port),
            ],
            cwd=str(ROOT / b["dir"]),
            stdout=log_fp,
            stderr=subprocess.STDOUT,
        )
        p.owns_server = True

        deadline = time.time() + int(b["start_timeout"])
        last_err = "探针未执行"
        while time.time() < deadline:
            if p.server.poll() is not None:
                tail = (p.work_dir / "backend.log").read_text(encoding="utf-8", errors="replace")[-1200:]
                raise StepFailed(f"后端进程提前退出（退出码 {p.server.returncode}），日志末尾：\n{tail}")
            ok, err = health_probe(url)
            if ok:
                p.say(f"健康检查通过：{url}")
                return
            last_err = err
            time.sleep(0.5)
        raise StepFailed(f"{int(b['start_timeout'])}s 内健康检查未通过：{url}（最后错误：{last_err}）")

    @p.step("complaints", "诉求接口条数/明细与台账一致性")
    def step_complaints(p: Pipeline) -> None:
        c = m["complaints"]
        b = m["backend"]
        base = f"http://{b['host']}:{b['port']}"
        if not health_ok(f"{base}{b['health_path']}"):
            raise StepFailed("后端服务未运行或健康检查未通过，请先让 health 步骤通过")
        size = int(c["page_size"])

        def get_json(path: str) -> Any:
            try:
                with urllib.request.urlopen(base + path, timeout=5) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                raise StepFailed(f"请求 {path} 失败：{exc}") from exc

        expected = json.loads(
            (p.work_dir / "complaint_seed_snapshot.json").read_text(encoding="utf-8")
        )
        if not expected:
            raise StepFailed("本次回填快照为空，请先让 seed 步骤通过")

        # 分页拉全量，验证 total 与实际条数一致
        items: list[dict[str, Any]] = []
        page = 1
        total: int | None = None
        while True:
            data = get_json(f"{c['list_path']}?page={page}&size={size}")
            if total is None:
                total = int(data.get("total", -1))
            batch = data.get("items", [])
            if not isinstance(batch, list):
                raise StepFailed(f"{c['list_path']} 返回的 items 不是列表")
            items.extend(batch)
            if len(batch) < size:
                break
            page += 1
            if page > 1000:
                raise StepFailed("分页数异常（超过 1000 页），疑似接口死循环")

        if total is None:
            raise StepFailed(f"{c['list_path']} 返回中没有 total 字段")
        if total != len(expected):
            raise StepFailed(
                f"接口 total={total}，台账清单 {len(expected)} 条，条数不一致"
            )
        if len(items) != total:
            raise StepFailed(
                f"接口 total={total}，但分页实际返回 {len(items)} 条，total 与列表不一致"
            )

        # 顺序必须是采集顺序；逐条核对本次回填快照要求的字段
        for i, (got, want) in enumerate(zip(items, expected), 1):
            got_id = got.get("诉求编号")
            want_id = want["诉求编号"]
            if got_id != want_id:
                raise StepFailed(
                    f"第 {i} 条顺序不一致：接口为 {got_id}，按采集顺序应为 {want_id}"
                )
            for field in [*c["required_fields"], "诉求状态", "采集时间"]:
                if str(got.get(field, "")) != str(want[field]):
                    raise StepFailed(
                        f"{want_id} 字段「{field}」不一致：接口={got.get(field)!r}，台账={want[field]!r}"
                    )
            if got.get("status") != want["status"]:
                raise StepFailed(
                    f"{want_id} 状态不一致：接口={got.get('status')!r}，台账={want['status']!r}"
                )
            if bool(got.get("pending")) != bool(want["pending"]):
                raise StepFailed(
                    f"{want_id} pending 标记不一致：接口={got.get('pending')!r}，台账={want['pending']!r}"
                )

        # 导出接口口径也要与台账一致
        exported = get_json(c["export_path"])
        if int(exported.get("total", -1)) != len(expected):
            raise StepFailed(
                f"导出接口 total={exported.get('total')}，与台账 {len(expected)} 条不一致"
            )
        if len(exported.get("items", [])) != len(expected):
            raise StepFailed("导出接口 items 条数与台账不一致")

        p.say(f"接口 {total} 条 == 台账 {len(expected)} 条，顺序与字段全部一致")


def sock_busy(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def health_probe(url: str) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            if resp.status == 200:
                return True, ""
            return False, f"HTTP {resp.status}"
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)


def health_ok(url: str) -> bool:
    return health_probe(url)[0]


# ---------- 入口 ----------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="公众诉求发布前检查流水线")
    ap.add_argument("--reset", action="store_true", help="丢弃上次进度，从头跑")
    ap.add_argument("--from-step", help="忽略该步及之后的旧进度，从指定步骤开始")
    ap.add_argument("--status", action="store_true", help="只打印各步骤通过状态")
    ap.add_argument("--manifest", default=str(ROOT / "release-check" / "manifest.toml"))
    args = ap.parse_args(argv)

    manifest = tomllib.loads(Path(args.manifest).read_text(encoding="utf-8"))
    p = Pipeline(manifest)
    register_steps(p)

    # 步骤顺序只以清单 pipeline.steps 为准；清单与实现不一致时直接报配置错误
    declared = list(manifest["pipeline"]["steps"])
    registered = {k: (t, fn) for k, t, fn in p.steps}
    missing = [k for k in declared if k not in registered]
    extra = [k for k in registered if k not in declared]
    if missing or extra:
        print(red("清单 pipeline.steps 与流水线实现不一致："))
        if missing:
            print(red("  清单声明但未实现：" + ", ".join(missing)))
        if extra:
            print(red("  已实现但清单未声明：" + ", ".join(extra)))
        return 2
    p.steps = [(k, *registered[k]) for k in declared]
    keys = [k for k, _, _ in p.steps]
    titles = dict((k, t) for k, t, _ in p.steps)

    state = {} if args.reset else p.load_state()
    if args.reset:
        p.reset_progress()
        state = {}
    if args.from_step:
        if args.from_step not in keys:
            print(red(f"未知步骤：{args.from_step}；可选：{', '.join(keys)}"))
            return 2
        cut = keys.index(args.from_step)
        state = {k: v for k, v in state.items() if k in keys[:cut]}

    if args.status:
        for k in keys:
            mark = green("PASS") if state.get(k) == "pass" else red("PENDING/FAIL")
            print(f"  [{mark}] {k} - {titles[k]}")
        return 0

    print(yellow("== 公众诉求发布前检查 =="))
    p.say(f"清单：{os.path.relpath(args.manifest, ROOT)}")
    # 运行时步骤依赖当下进程/接口状态，不能因为上次通过就跳过：
    # health 每次重新探针，complaints 每次重新对实时接口做校验
    runtime_steps = {"health", "complaints"}
    failed_key: str | None = None
    try:
        for key, title, fn in p.steps:
            if state.get(key) == "pass" and key not in runtime_steps:
                print(f"  [{green('SKIP')}] {key} - {title}（上次已通过）")
                continue
            if key in runtime_steps:
                # 运行时步骤的旧日志属于中间产物，重跑前清掉
                (p.work_dir / "backend.log").unlink(missing_ok=True)
            print(f"  [{yellow('RUN ')}] {key} - {title}")
            try:
                # 每步重跑前先清掉自己这步的旧中间产物（脚本内随步骤处理）
                fn(p)
            except StepFailed as exc:
                print(f"  [{red('FAIL')}] {key} - {title}")
                for line in str(exc).splitlines():
                    print(f"         {red(line)}")
                failed_key = key
                state[key] = "fail"
                p.save_state(state)
                print(red(f"\n停在「{title}」。修好原因后重新执行本命令即可从这一步继续；"))
                print(f"详细日志：{os.path.relpath(p.log_path, ROOT)}")
                return 1
            state[key] = "pass"
            p.save_state(state)
            print(f"  [{green('PASS')}] {key} - {title}")
        print(green("\n全部检查通过，可以发布。"))
        return 0
    finally:
        p.close()


if __name__ == "__main__":
    sys.exit(main())
