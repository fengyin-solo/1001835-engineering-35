#!/usr/bin/env python3
"""公众诉求模块发布前检查流程。

检查步骤只以 release-check/manifest.json 这一处清单为准：
本脚本不硬编码任何步骤，Makefile 与 README 也只负责调用本脚本。

用法：
    python3 release-check/run_checks.py            # 续跑：从上次失败/变化的步骤开始
    python3 release-check/run_checks.py --fresh    # 清理全部中间产物后从头跑
    python3 release-check/run_checks.py --from env # 从指定步骤及其后续步骤重跑
    python3 release-check/run_checks.py --list     # 只打印清单中的步骤

中间产物全部隔离在 .release-check/ 下（已 gitignore）：
    run/    本次运行的产物（回填的示例数据等）
    steps/  每步的通过戳与详细日志
    state.json  各输入文件的指纹，用于判断缓存是否失效
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "release-check" / "manifest.json"
STATE_DIR = REPO_ROOT / ".release-check"
RUN_DIR = STATE_DIR / "run"
STEPS_DIR = STATE_DIR / "steps"
STATE_FILE = STATE_DIR / "state.json"
SEED_OUT = RUN_DIR / "complaint.seed.json"

# ---------------------------------------------------------------------------
# 输出与异常
# ---------------------------------------------------------------------------

def _supports_color() -> bool:
    return sys.stdout.isatty() and os.getenv("NO_COLOR") is None


COLOR = _supports_color()


def paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def info(msg: str) -> None:
    print(msg)


def detail(msg: str) -> None:
    print(f"       {msg}")


class StepFailed(Exception):
    """某一步检查未通过；message 要写到让人直接知道原因。"""


# ---------------------------------------------------------------------------
# 指纹与断点续跑状态
# ---------------------------------------------------------------------------

def file_fingerprint(path: Path) -> str:
    if path.is_file():
        h = hashlib.sha256()
        h.update(path.read_bytes())
        return h.hexdigest()
    if path.is_dir():
        h = hashlib.sha256()
        for child in sorted(path.rglob("*")):
            if not child.is_file():
                continue
            rel = child.relative_to(path).as_posix()
            if any(part in {"__pycache__", ".venv"} for part in child.parts):
                continue
            h.update(rel.encode())
            h.update(b"\0")
            h.update(child.read_bytes())
            h.update(b"\0")
        return h.hexdigest()
    raise FileNotFoundError(path)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def step_log_path(step_id: str) -> Path:
    return STEPS_DIR / f"{step_id}.log"


def write_step_log(step_id: str, content: str) -> None:
    STEPS_DIR.mkdir(parents=True, exist_ok=True)
    step_log_path(step_id).write_text(content, encoding="utf-8")


def marker(step_id: str, status: str, detail: str) -> None:
    STEPS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "detail": detail,
    }
    (STEPS_DIR / f"{step_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_marker(step_id: str) -> dict | None:
    path = STEPS_DIR / f"{step_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise StepFailed(f"文件不存在：{path.relative_to(REPO_ROOT)}")
    except json.JSONDecodeError as exc:
        raise StepFailed(f"{path.relative_to(REPO_ROOT)} 不是合法 JSON：{exc}")


def parse_version(text: str) -> tuple[int, ...]:
    match = re.match(r"\s*(\d+(?:\.\d+)*)", text)
    if not match:
        raise StepFailed(f"无法解析版本号：{text!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def http_get_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# 步骤 1：后端依赖版本
# ---------------------------------------------------------------------------

_REQ_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)(?:\[[A-Za-z0-9_,\-]+\])?\s*(>=|==|<=|>|<|~=)?\s*([^\s;]+)?")


def parse_requirements(path: Path) -> list[tuple[str, str, str]]:
    specs: list[tuple[str, str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = _REQ_RE.match(line)
        if not match:
            raise StepFailed(f"requirements.txt 中存在无法解析的依赖行：{raw!r}")
        name, op, want = match.group(1), match.group(2) or ">=", match.group(3) or "0"
        specs.append((name, op, want))
    return specs


def check_deps(ctx: dict) -> str:
    conf = ctx["manifest"]["backend"]
    req_path = REPO_ROOT / conf["requirements"]
    specs = parse_requirements(req_path)

    venv_python = REPO_ROOT / conf["dir"] / ".venv" / "bin" / "python"
    if not venv_python.exists():
        raise StepFailed(
            f"后端虚拟环境不存在（{venv_python.relative_to(REPO_ROOT)}）。"
            "先执行 make install 准备依赖环境后，再续跑本流程。"
        )

    query = (
        "import importlib.metadata as m, json;"
        "print(json.dumps({d.metadata['Name'].lower(): d.version "
        "for d in m.distributions()}))"
    )
    proc = subprocess.run(
        [str(venv_python), "-c", query],
        cwd=REPO_ROOT / conf["dir"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise StepFailed(
            "无法读取虚拟环境中已安装的依赖版本：\n" + proc.stderr.strip()
        )
    installed = json.loads(proc.stdout)

    failures: list[str] = []
    passed: list[str] = []
    for name, op, want in specs:
        actual = installed.get(name.lower())
        if actual is None:
            failures.append(f"{name} 要求 {op}{want}，但虚拟环境中未安装")
            continue
        if op == "~=":
            op = ">="  # 兼容写法，当前 requirements 未使用
        ok = {
            ">=": parse_version(actual) >= parse_version(want),
            "<=": parse_version(actual) <= parse_version(want),
            "==": parse_version(actual) == parse_version(want),
            ">": parse_version(actual) > parse_version(want),
            "<": parse_version(actual) < parse_version(want),
        }[op]
        if ok:
            passed.append(f"{name} {actual}（要求 {op}{want}）")
        else:
            failures.append(f"{name} 要求 {op}{want}，实际安装 {actual}，不满足")

    write_step_log("deps", "\n".join(passed) + "\n")
    if failures:
        raise StepFailed(
            "后端依赖版本核对未通过：\n  - "
            + "\n  - ".join(failures)
            + "\n请在 backend/ 下按 requirements.txt 更新虚拟环境后，从本步续跑。"
        )
    return "、".join(passed)


# ---------------------------------------------------------------------------
# 步骤 2：按采集顺序回填诉求示例数据
# ---------------------------------------------------------------------------

def load_ledger(conf: dict) -> list[dict]:
    """读取并校验诉求台账，返回原始记录。"""
    ledger_path = REPO_ROOT / conf["ledger"]
    ledger = read_json(ledger_path)
    if not isinstance(ledger, list) or not ledger:
        raise StepFailed(f"{conf['ledger']} 必须是非空数组，当前无法作为诉求台账使用")

    required = conf["required_fields"]
    allowed_status = set(conf["allowed_status"])
    seen_times: set[str] = set()
    problems: list[str] = []
    for index, item in enumerate(ledger, start=1):
        if not isinstance(item, dict):
            problems.append(f"第 {index} 条不是对象")
            continue
        for field in required:
            if not str(item.get(field) or "").strip():
                problems.append(f"第 {index} 条缺少必填字段「{field}」")
        status = item.get("status")
        if status not in allowed_status:
            problems.append(
                f"第 {index} 条 status={status!r}，合法值为：{'、'.join(conf['allowed_status'])}"
            )
        raw_time = str(item.get("采集时间") or "").strip()
        try:
            datetime.fromisoformat(raw_time)
        except ValueError:
            problems.append(f"第 {index} 条「采集时间」不是合法 ISO 时间：{raw_time!r}")
            continue
        if raw_time in seen_times:
            problems.append(f"第 {index} 条「采集时间」与其他记录重复：{raw_time}")
        seen_times.add(raw_time)

    if problems:
        raise StepFailed(
            "诉求台账内容核对未通过：\n  - " + "\n  - ".join(problems)
        )
    return ledger


def build_seed_rows(conf: dict, ledger: list[dict]) -> list[dict]:
    """按采集时间排序，把台账记录转成后端可直接加载的示例数据行。"""
    ordered = sorted(ledger, key=lambda item: datetime.fromisoformat(item["采集时间"]))
    terminal = set(conf["terminal_status"])
    prefix = conf["code_prefix"]
    rows: list[dict] = []
    for i, item in enumerate(ordered, start=1):
        row = {"id": i, "诉求编号": f"{prefix}-{i:04d}"}
        for key, value in item.items():
            row[key] = value
        row["status"] = item["status"]
        row["诉求状态"] = item["status"]
        row["pending"] = item["status"] not in terminal
        row["abnormal"] = False
        rows.append(row)
    return rows


def status_summary(conf: dict, rows: list[dict]) -> str:
    return "、".join(
        f"{name}{sum(1 for r in rows if r['status'] == name)}条"
        for name in conf["allowed_status"]
        if any(r["status"] == name for r in rows)
    )


def check_seed(ctx: dict) -> str:
    conf = ctx["manifest"]["complaint"]
    rows = build_seed_rows(conf, load_ledger(conf))

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SEED_OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SEED_OUT)

    return (
        f"已按采集时间排序回填 {len(rows)} 条（{status_summary(conf, rows)}），"
        f"区间 {rows[0]['采集时间']} ~ {rows[-1]['采集时间']}，"
        f"产物 {SEED_OUT.relative_to(REPO_ROOT)}"
    )


# ---------------------------------------------------------------------------
# docker-compose / Dockerfile 的轻量解析（不引入第三方依赖）
# ---------------------------------------------------------------------------

def parse_compose(path: Path) -> dict[str, dict]:
    """只提取本流程需要的 services.<name>.ports / environment，够用即可。"""
    services: dict[str, dict] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    in_services = False
    current: str | None = None
    current_indent = None
    list_key: str | None = None

    def flush():
        nonlocal current, list_key
        current = None
        list_key = None

    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        body = raw.strip()

        if indent == 0:
            in_services = body == "services:"
            flush()
            continue
        if not in_services:
            continue
        if indent == 2 and body.endswith(":"):
            current = body[:-1]
            current_indent = indent
            services[current] = {"ports": [], "environment": []}
            list_key = None
            continue
        if current is None:
            continue
        if indent <= (current_indent or 2):
            flush()
            continue
        if indent == 4 and body.endswith(":"):
            list_key = body[:-1]
            continue
        if body.startswith("- ") and list_key in {"ports", "environment"}:
            value = body[2:].strip().strip('"').strip("'")
            services[current][list_key].append(value)
            continue
        if list_key == "environment" and ":" in body:
            key = body.split(":", 1)[0].strip()
            services[current]["environment"].append(key)
    return services


def dockerfile_expose(path: Path) -> set[int]:
    if not path.exists():
        raise StepFailed(f"Dockerfile 不存在：{path.relative_to(REPO_ROOT)}")
    ports: set[int] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*EXPOSE\s+(.+)", raw, flags=re.IGNORECASE)
        if match:
            for token in match.group(1).split():
                ports.add(int(token.split("/")[0]))
    return ports


# ---------------------------------------------------------------------------
# 步骤 3：容器端口
# ---------------------------------------------------------------------------

def check_ports(ctx: dict) -> str:
    manifest = ctx["manifest"]
    compose = parse_compose(REPO_ROOT / manifest["compose_file"])
    failures: list[str] = []
    passed: list[str] = []

    for expected in manifest["ports"]:
        service = expected["service"]
        if service not in compose:
            failures.append(f"docker-compose.yml 中缺少服务「{service}」")
            continue
        mappings = compose[service]["ports"]
        pairs: list[tuple[int, int]] = []
        for mapping in mappings:
            match = re.match(
                r"^\s*(?:\d{1,3}(?:\.\d{1,3}){3}:)?(?P<host>\d+)\s*:\s*(?P<container>\d+)(?:/[a-z]+)?\s*$",
                mapping,
            )
            if match:
                pairs.append((int(match.group("host")), int(match.group("container"))))
        wanted = (expected["host"], expected["container"])
        if wanted not in pairs:
            failures.append(
                f"服务「{service}」端口映射缺少 {wanted[0]}:{wanted[1]}，"
                f"compose 实际声明：{mappings or '无'}"
            )
        else:
            passed.append(f"{service} 端口映射 {wanted[0]}:{wanted[1]}")

        dockerfile_key = "dockerfile"
        subconf = manifest.get(service, {})
        dockerfile = REPO_ROOT / subconf.get(dockerfile_key, f"{service}/Dockerfile")
        exposed = dockerfile_expose(dockerfile)
        if expected["container"] not in exposed:
            failures.append(
                f"{dockerfile.relative_to(REPO_ROOT)} 未 EXPOSE 容器端口 "
                f'{expected["container"]}，实际 EXPOSE：{sorted(exposed) or "无"}'
            )
        else:
            passed.append(f"{dockerfile.relative_to(REPO_ROOT)} EXPOSE {expected['container']}")

    if failures:
        raise StepFailed("容器端口核对未通过：\n  - " + "\n  - ".join(failures))
    return "；".join(passed)


# ---------------------------------------------------------------------------
# 步骤 4：环境变量
# ---------------------------------------------------------------------------

def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def check_env(ctx: dict) -> str:
    manifest = ctx["manifest"]
    compose = parse_compose(REPO_ROOT / manifest["compose_file"])
    example = parse_env_file(REPO_ROOT / manifest["env_example"])
    failures: list[str] = []
    passed: list[str] = []

    for service, required in manifest["backend"]["required_env"].items():
        service_env = set(compose.get(service, {}).get("environment", []))
        for name in required:
            in_compose = any(item.split("=", 1)[0] == name for item in service_env)
            if not in_compose:
                failures.append(
                    f"docker-compose.yml 的服务「{service}」缺少环境变量 {name}"
                )
            if name not in example:
                failures.append(
                    f"{manifest['env_example']} 缺少 {name} 的示例声明"
                )
            if in_compose and name in example:
                passed.append(f"{service}.{name} 在 compose 与 .env.example 中均已声明")

    if failures:
        raise StepFailed("环境变量核对未通过：\n  - " + "\n  - ".join(failures))
    return "；".join(passed)


# ---------------------------------------------------------------------------
# 步骤 5：起服务，核对诉求条数与台账一致
# ---------------------------------------------------------------------------

class ApiServer:
    def __init__(self, port: int, log_path: Path) -> None:
        self.port = port
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None

    def start(self, backend_dir: Path) -> None:
        env = os.environ.copy()
        env["APP_ENV"] = "release-check"
        env["APP_PORT"] = str(self.port)
        env["APP_SEED_FILE"] = str(SEED_OUT)
        log_fh = self.log_path.open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [".venv/bin/python", "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=backend_dir,
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
        self.log_fh = log_fh

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if getattr(self, "log_fh", None):
            self.log_fh.close()


def pick_port(preferred: int) -> int:
    for candidate in [preferred, 0]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", candidate))
            chosen = sock.getsockname()[1]
            sock.close()
            return chosen
        except OSError:
            sock.close()
    raise StepFailed("找不到可用的本地端口启动后端服务")


def check_api(ctx: dict) -> str:
    manifest = ctx["manifest"]
    conf = manifest["complaint"]

    # 期望值一律以台账为准，不以上一步留下的中间产物为准；
    # 这样即使 .release-check/run 里的文件被污染，也会在这里暴露。
    expected_rows = build_seed_rows(conf, load_ledger(conf))
    expected_count = len(expected_rows)
    expected_codes = [row["诉求编号"] for row in expected_rows]
    expected_times = [row["采集时间"] for row in expected_rows]
    expected_by_status = {
        name: sum(1 for row in expected_rows if row["status"] == name)
        for name in conf["allowed_status"]
    }

    if not SEED_OUT.exists():
        raise StepFailed(
            f"缺少回填产物 {SEED_OUT.relative_to(REPO_ROOT)}，"
            "请不要跳过「按采集顺序回填诉求示例数据」步骤"
        )
    try:
        artifact_rows = read_json(SEED_OUT)
    except StepFailed:
        artifact_rows = None
    if artifact_rows != expected_rows:
        raise StepFailed(
            f"回填产物 {SEED_OUT.relative_to(REPO_ROOT)} 与台账当前内容不一致"
            f"（台账 {expected_count} 条，产物 "
            f"{len(artifact_rows) if isinstance(artifact_rows, list) else '无法解析'} 条），"
            "疑似被上一次运行或人工修改污染。用 --fresh 重跑即可从回填步骤重新生成。"
        )

    backend_dir = REPO_ROOT / manifest["backend"]["dir"]
    port = pick_port(manifest["ports"][0]["host"])
    server = ApiServer(port, step_log_path("api"))
    atexit.register(server.stop)

    def _stop_on_signal(signum, frame):
        server.stop()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _stop_on_signal)

    server.start(backend_dir)
    base = f"http://127.0.0.1:{port}"
    health = manifest["complaint"]["health_path"]
    deadline = time.time() + 20
    last_error = ""
    while time.time() < deadline:
        if server.proc.poll() is not None:
            tail = step_log_path("api").read_text(encoding="utf-8")[-1500:]
            raise StepFailed(f"后端进程提前退出，uvicorn 日志末尾：\n{tail}")
        try:
            status, body = http_get_json(base + health)
            if status == 200 and body.get("ok"):
                break
        except (urllib.error.URLError, ConnectionError, OSError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        time.sleep(0.4)
    else:
        tail = step_log_path("api").read_text(encoding="utf-8")[-1500:]
        raise StepFailed(
            f"后端在 20 秒内未通过健康检查（{health}，最后错误：{last_error}）。"
            f"uvicorn 日志末尾：\n{tail}"
        )

    try:
        failures: list[str] = []
        list_url = (
            base + manifest["complaint"]["list_path"]
            + f"?page=1&size={min(200, expected_count)}"
        )
        try:
            _, list_body = http_get_json(list_url)
        except urllib.error.HTTPError as exc:
            raise StepFailed(f"诉求列表接口请求失败：HTTP {exc.code} {exc.reason}")
        list_items = list_body.get("items")
        list_total = list_body.get("total")
        if list_total != expected_count:
            failures.append(
                f"列表接口 total={list_total}，与台账条数 {expected_count} 不一致"
            )
        if not isinstance(list_items, list) or len(list_items) != expected_count:
            failures.append(
                f"列表接口返回条数={len(list_items) if isinstance(list_items, list) else '非数组'}，"
                f"与台账条数 {expected_count} 不一致"
            )
        else:
            actual_codes = [row["诉求编号"] for row in list_items]
            if actual_codes != expected_codes:
                failures.append(
                    "列表返回顺序与采集顺序不一致："
                    f"接口前几条 {actual_codes[:3]}，台账前几条 {expected_codes[:3]}"
                )
            actual_times = [row.get("采集时间") for row in list_items]
            if actual_times != expected_times:
                failures.append("列表记录的「采集时间」序列与台账排序结果不一致")

        # 导出接口同样以台账为准；此前该路由被 /{entry_id} 抢匹配会返回 422
        try:
            export_status, export_body = http_get_json(
                base + manifest["complaint"]["export_path"]
            )
        except urllib.error.HTTPError as exc:
            raise StepFailed(
                f"诉求导出接口 {manifest['complaint']['export_path']} 返回 "
                f"HTTP {exc.code}（路由可能被其他路径抢先匹配）"
            )
        if export_body.get("total") != expected_count:
            failures.append(
                f"导出接口 total={export_body.get('total')}，与台账条数 {expected_count} 不一致"
            )
        export_items = export_body.get("items")
        if not isinstance(export_items, list) or len(export_items) != expected_count:
            failures.append("导出接口返回条数与台账条数不一致")
        elif [row["诉求编号"] for row in export_items] != expected_codes:
            failures.append("导出接口记录集合/顺序与列表接口不一致")

        # 各状态筛选口径相加应等于全量，避免状态值写花导致台账对不上
        counted = 0
        for name, want_n in expected_by_status.items():
            try:
                _, body = http_get_json(
                    base + manifest["complaint"]["list_path"]
                    + f"?status={urllib.parse.quote(name)}&page=1&size=200"
                )
            except urllib.error.HTTPError as exc:
                failures.append(f"按状态「{name}」筛选请求失败：HTTP {exc.code}")
                continue
            got_n = body.get("total")
            counted += got_n or 0
            if got_n != want_n:
                failures.append(
                    f"状态「{name}」筛选 total={got_n}，台账中为 {want_n}"
                )
        if not failures and counted != expected_count:
            failures.append(
                f"各状态筛选条数之和 {counted} 不等于台账全量 {expected_count}"
            )

        if failures:
            raise StepFailed(
                "诉求条数/顺序与台账核对未通过：\n  - " + "\n  - ".join(failures)
            )
        port_note = "" if port == manifest["ports"][0]["host"] else f"（8000 被占用，实际使用 {port}）"
        return (
            f"服务健康检查通过{port_note}；列表、导出接口均返回 {expected_count} 条，"
            f"与台账一致且顺序等于采集顺序；各状态筛选合计 {counted} 条"
        )
    finally:
        server.stop()
        atexit.unregister(server.stop)


# ---------------------------------------------------------------------------
# 流程调度
# ---------------------------------------------------------------------------

HANDLERS = {
    "check_deps": check_deps,
    "check_seed": check_seed,
    "check_ports": check_ports,
    "check_env": check_env,
    "check_api": check_api,
}


def list_steps(manifest: dict) -> None:
    info(paint("1", f"清单：{manifest['module']} 发布前检查（{MANIFEST_PATH.relative_to(REPO_ROOT)}）"))
    for i, step in enumerate(manifest["steps"], start=1):
        print(f"  {i}. [{step['id']}] {step['name']}")


def run(manifest: dict, fresh: bool, from_step: str | None) -> int:
    if fresh and STATE_DIR.exists():
        shutil.rmtree(STATE_DIR)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    STEPS_DIR.mkdir(parents=True, exist_ok=True)

    steps = manifest["steps"]
    ids = [step["id"] for step in steps]
    if from_step and from_step not in ids:
        info(f"未知步骤 {from_step!r}，可选：{', '.join(ids)}")
        return 2
    from_index = ids.index(from_step) if from_step else 0
    # 指定 --from 时，该步骤及其后续步骤的旧戳先作废并强制重跑；更早的步骤沿用缓存。
    forced_start = ids.index(from_step) if from_step else len(ids)
    if from_step:
        for sid in ids[from_index:]:
            (STEPS_DIR / f"{sid}.json").unlink(missing_ok=True)

    state = load_state()
    state.setdefault("fingerprints", {})

    ctx = {"manifest": manifest}
    title = paint("1;36", f"公众诉求发布前检查 · {datetime.now():%Y-%m-%d %H:%M:%S}")
    info(title)
    any_ran = False
    failed: dict | None = None

    for index, step in enumerate(steps):
        sid = step["id"]
        name = step["name"]
        prefix = paint("1;36", f"[{index + 1}/{len(steps)}]")

        input_paths = [MANIFEST_PATH] + [REPO_ROOT / p for p in step.get("inputs", [])]
        fingerprint_parts = []
        missing_input = None
        for path in input_paths:
            try:
                fingerprint_parts.append(f"{path.relative_to(REPO_ROOT)}:{file_fingerprint(path)}")
            except FileNotFoundError:
                missing_input = path.relative_to(REPO_ROOT)
        fingerprint = hashlib.sha256("\n".join(fingerprint_parts).encode()).hexdigest()

        previous = read_marker(sid)
        cached = (
            index < forced_start
            and previous
            and previous.get("status") == "passed"
            and state["fingerprints"].get(sid) == fingerprint
            and not any_ran
        )
        if cached:
            skip_tag = paint("90", "跳过")
            info(f"{prefix} {skip_tag} {name}")
            detail(f"与上次通过时一致（{previous.get('detail', '')}）")
            continue

        any_ran = True
        run_tag = paint("1;33", "执行")
        info(f"{prefix} {run_tag} {name}")
        if missing_input:
            failed = {"step": step, "message": f"检查所需输入不存在：{missing_input}"}
            break

        handler = HANDLERS.get(step["handler"])
        if handler is None:
            failed = {"step": step, "message": f"manifest 引用了未实现的处理器 {step['handler']}"}
            break

        try:
            result = handler(ctx)
        except StepFailed as exc:
            failed = {"step": step, "message": str(exc)}
            break
        except Exception as exc:  # 防御性：保证异常也能指出是哪一步
            failed = {"step": step, "message": f"检查脚本自身异常：{type(exc).__name__}: {exc}"}
            break

        state["fingerprints"][sid] = fingerprint
        save_state(state)
        marker(sid, "passed", result)
        info(f"       {paint('32', '通过')} {result}")

    if failed:
        step = failed["step"]
        marker(step["id"], "failed", failed["message"])
        info("")
        info(paint("1;31", f"未通过步骤：{step['name']}（{step['id']}）"))
        for line in failed["message"].splitlines():
            info(paint("31", f"  {line}"))
        log = step_log_path(step["id"])
        if log.exists():
            info(paint("90", f"  详细日志：{log.relative_to(REPO_ROOT)}"))
        info("")
        info(paint("1;33", "修好后直接重跑本命令，将从这一步继续；如需从头开始用 --fresh。"))
        return 1

    info("")
    if any_ran:
        info(paint("1;32", "全部检查通过，可以发布。"))
    else:
        info(paint("32", "全部步骤与上次通过时一致，无需重跑（用 --fresh 可强制全量检查）。"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="公众诉求模块发布前检查流程")
    parser.add_argument("--fresh", action="store_true", help="清理 .release-check 后从头执行")
    parser.add_argument("--from", dest="from_step", help="从指定步骤开始（含后续步骤）重跑")
    parser.add_argument("--list", action="store_true", help="只打印 manifest 中的步骤清单")
    args = parser.parse_args()

    manifest = read_json(MANIFEST_PATH)
    if args.list:
        list_steps(manifest)
        return 0
    return run(manifest, fresh=args.fresh, from_step=args.from_step)


if __name__ == "__main__":
    sys.exit(main())
