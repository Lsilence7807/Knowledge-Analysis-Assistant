# 文件：backend/app/services/sandbox.py
# 作用：受限代码执行：RestrictedPython 编译期守卫 + `python -I` 隔离子进程 + 项目内临时工作目录 + 超时 + 输出上限，
#       只把当前数据集的只读副本（dataset.csv）交给用户代码，代码里拿不到密钥、网络与项目其它文件
# 阶段：P15 沙箱代码执行与指标语义层
#       F7 手写 AST 逃逸检查换 RestrictedPython 编译期守卫（子进程、超时、临时目录三件事不动）
# 依赖：RestrictedPython、标准库 ast（只用于 import 白名单与禁用调用）、operator、os、re、shutil、
#       subprocess、sys、time、uuid、contextlib、pathlib、
#       backend/app/core/config.py、backend/app/core/db.py、backend/app/core/exec.py
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from RestrictedPython import compile_restricted

from app.core import config, db, exec
from app.services import store

ENABLED = os.getenv("ENABLE_SANDBOX", "false").lower() == "true"
TIMEOUT_S = int(os.getenv("SANDBOX_TIMEOUT_S", "20"))
MAX_OUTPUT = 1_000_000
WORK_DIR = "sandbox"

# 只放数据计算类库：能碰文件、网络、进程或动态导入的模块一律不在列（os/sys/subprocess/socket/pathlib/importlib…）
ALLOWED_IMPORTS = frozenset(
    {
        "pandas",
        "numpy",
        "math",
        "statistics",
        "decimal",
        "fractions",
        "random",
        "datetime",
        "time",
        "json",
        "re",
        "csv",
        "io",
        "textwrap",
        "string",
        "collections",
        "itertools",
        "functools",
        "operator",
        "heapq",
        "bisect",
        "typing",
        "dataclasses",
        "enum",
        "uuid",
    }
)
# 文件、动态执行与交互入口：静态检查先挡一道，子进程里再从 builtins 里摘掉（两道都过不去）
BANNED_CALLS = frozenset({"open", "eval", "exec", "compile", "__import__", "input", "breakpoint"})
# RestrictedPython 的 safe_builtins 很保守（连 print/sum/bytearray 都没有），按「数据计算够用」补一小撮；
# 补进来的全是纯计算与容器构造，碰文件、进程、网络与自省的一个都不加
EXTRA_BUILTINS = (
    "all",
    "any",
    "bytearray",
    "dict",
    "enumerate",
    "filter",
    "format",
    "frozenset",
    "iter",
    "list",
    "map",
    "max",
    "min",
    "next",
    "print",
    "reversed",
    "set",
    "sum",
    "type",
)
DATASET_ID_CHARS = re.compile("[A-Za-z0-9_]+")

# 沙箱执行器：由 run_code 写进工作目录，子进程只认 argv（用户代码路径、数据集副本路径），不读环境变量
RUNNER = """# 沙箱执行器（由 app/sandbox.py 生成，勿手改）
import builtins
import operator
import sys
import traceback

import numpy as np
import pandas as pd
from RestrictedPython import PrintCollector, compile_restricted, safe_builtins
from RestrictedPython.Guards import (
    full_write_guard,
    guarded_iter_unpack_sequence,
    guarded_unpack_sequence,
    safer_getattr,
)

ALLOWED = __ALLOWED__
EXTRA = __EXTRA__
_real_import = builtins.__import__
# += 这类原地运算由 RestrictedPython 编译成 _inplacevar_ 调用，框架不提供实现，只能在这里按运算符表挂
INPLACE = {
    "+=": operator.iadd,
    "-=": operator.isub,
    "*=": operator.imul,
    "/=": operator.itruediv,
    "//=": operator.ifloordiv,
    "%=": operator.imod,
    "**=": operator.ipow,
    "&=": operator.iand,
    "|=": operator.ior,
    "^=": operator.ixor,
    ">>=": operator.irshift,
    "<<=": operator.ilshift,
}


def _guard_import(name, *args, **kwargs):
    if str(name).split(".")[0] not in ALLOWED:
        raise ImportError("沙箱不允许导入 " + str(name))
    return _real_import(name, *args, **kwargs)


def _inplacevar_(op, left, right):
    return INPLACE[op](left, right)


safe = {**safe_builtins, **{name: getattr(builtins, name) for name in EXTRA if hasattr(builtins, name)}}
safe["__import__"] = _guard_import

code_path, data_path = sys.argv[1], sys.argv[2]
namespace = {
    "__builtins__": safe,
    "__name__": "__main__",
    # RestrictedPython 的守卫插口：属性、下标、赋值、迭代、解包与类定义各挂一个
    "_getattr_": safer_getattr,
    "_getitem_": operator.getitem,
    "_write_": full_write_guard,
    "_getiter_": iter,
    "_print_": PrintCollector,
    "_unpack_sequence_": guarded_unpack_sequence,
    "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
    "_inplacevar_": _inplacevar_,
    "__metaclass__": type,
    "pd": pd,
    "np": np,
}
if data_path:
    namespace["df"] = pd.read_csv(data_path)
with open(code_path, encoding="utf-8") as handle:
    source = handle.read()
try:
    exec(compile_restricted(source, "user_code.py", "exec"), namespace)
except BaseException:
    traceback.print_exc()
    sys.exit(1)
# print 被 RestrictedPython 收进 _print_ 收集器，这里再倒回真实 stdout（顺序在 result 之前）
collected = namespace.get("_print")
if callable(collected):
    sys.stdout.write(str(collected()))
if namespace.get("result") is not None:
    print(namespace["result"])
"""


class SandboxError(RuntimeError):
    """代码被拒或沙箱不可用（语法错、越权 import、危险调用、数据集不存在）；信息可直接回给模型或用户。"""


def _check_module(name: str) -> None:
    """import 白名单：按顶级模块名判定，`pandas.core` 这类子模块跟着 `pandas` 一起放行。"""
    if name.split(".")[0] not in ALLOWED_IMPORTS:
        raise SandboxError(f"沙箱不允许导入 {name}（只放行 pandas/numpy 等数据计算库，不放行文件、网络与进程模块）")


def check_code(code: str) -> None:
    """静态检查：语法与逃逸交给 RestrictedPython 编译期守卫，import 白名单与禁用调用仍是本层策略；
    任一不过抛 SandboxError，不启动子进程。"""
    if not (code or "").strip():
        raise SandboxError("代码是空的")
    try:
        tree = ast.parse(code, filename="user_code.py", mode="exec")
    except SyntaxError as exc:
        raise SandboxError(f"代码语法错误：{exc.msg}（第 {exc.lineno} 行）") from exc
    try:
        # 下划线开头的名字与属性、__import__、绕过内建限制的写法都在这里判死（§3.3 逃逸清单的静态那半）
        compile_restricted(code, "user_code.py", "exec")
    except SyntaxError as exc:
        raise SandboxError(f"代码不允许：{exc.msg}（第 {exc.lineno} 行）") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_module(alias.name)
        elif isinstance(node, ast.ImportFrom):
            _check_module(node.module or "")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BANNED_CALLS:
            raise SandboxError(f"沙箱不允许调用 {node.func.id}()：文件读写与动态执行都不在能力范围内")


def _workdir() -> Path:
    """在项目数据目录下开一个一次性工作目录（§6：工作目录不落到项目外）。"""
    directory = Path(config.DATA_DIR) / WORK_DIR / uuid4().hex
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _dataset_copy(dataset_id: str, workdir: Path) -> Path:
    """把数据集在 DuckDB 里的表导出成一份只读副本 CSV；id 非法或数据集不存在抛 SandboxError。"""
    if not DATASET_ID_CHARS.fullmatch(dataset_id) or store.get_dataset(dataset_id) is None:
        raise SandboxError(f"数据集不存在：{dataset_id}")
    data_path = workdir / "dataset.csv"
    table = db.table_name(dataset_id)
    try:
        with closing(db.connect(read_only=True)) as conn:
            # 走 pandas 导出而不是 COPY TO：路径不用拼进 SQL，少一层转义，也天然是只读快照
            conn.execute(f'SELECT * FROM "{table}"').df().to_csv(data_path, index=False)
    except Exception as exc:  # noqa: BLE001  DuckDB 读错的各种姿势都归成一句可读的拒绝
        raise SandboxError(f"数据集副本导出失败：{exc}") from exc
    return data_path


def _read_capped(path: Path, limit: int) -> tuple[str, bool]:
    """按上限读文件（多读 1 字节用来判断有没有截断），返回 (文本, 是否截断)。"""
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    return raw[:limit].decode("utf-8", errors="replace"), len(raw) > limit


def _run_process(workdir: Path, code: str, data_path: Path | None, timeout_s: int, max_output: int) -> dict:
    """真正跑子进程的部分（只由 run_blocking 调用）；输出落文件再截断读，巨量输出不会撑爆父进程内存。"""
    runner = RUNNER.replace("__ALLOWED__", repr(sorted(ALLOWED_IMPORTS))).replace(
        "__EXTRA__", repr(sorted(EXTRA_BUILTINS))
    )
    (workdir / "runner.py").write_text(runner, encoding="utf-8", newline="")
    (workdir / "user_code.py").write_text(code, encoding="utf-8", newline="")
    argv = [
        sys.executable,
        "-I",  # 隔离模式：不读环境变量、不继承 PYTHONPATH、脚本目录不进 sys.path（§7）
        "-X",
        "utf8",  # 隔离模式会忽略 PYTHONIOENCODING，中文输出靠这个开关保证是 UTF-8
        str(workdir / "runner.py"),
        str(workdir / "user_code.py"),
        str(data_path or ""),
    ]
    # 只放子进程活命必需的几项：密钥、代理与自定义环境变量一概不进沙箱
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP") if key in os.environ}
    timed_out = False
    with (
        (workdir / "out.txt").open("w", encoding="utf-8") as out,
        (workdir / "err.txt").open("w", encoding="utf-8") as err,
    ):
        try:
            done = subprocess.run(argv, cwd=workdir, env=env, stdout=out, stderr=err, timeout=timeout_s, check=False)
            returncode = done.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = -1
    output, truncated = _read_capped(workdir / "out.txt", max_output)
    stderr, _ = _read_capped(workdir / "err.txt", max_output)
    if timed_out:
        error = f"沙箱超时（>{timeout_s}s）已终止进程"
    elif returncode:
        # 回给模型的是最后一行异常，完整 traceback 留在 stderr 里供排障
        error = stderr.strip().splitlines()[-1] if stderr.strip() else f"代码执行失败（退出码 {returncode}）"
    else:
        error = ""
    return {
        "ok": not returncode,
        "returncode": returncode,
        "output": output,
        "truncated": truncated,
        "stderr": stderr,
        "error": error,
    }


def run_code(code: str, timeout_s: int = 20, max_output: int = 1_000_000, *, dataset_id: str = "") -> dict:
    """执行一段 pandas 代码，返回 {ok, returncode, output, truncated, stderr, error, duration_ms}。

    dataset_id 非空时把该数据集的只读副本注入成 df；结果放进 result 变量就会被打印出来。
    代码被拒（语法/越权/危险调用）抛 SandboxError；超时与运行时报错只体现在返回值里，不抛。
    """
    if not ENABLED:
        raise SandboxError("代码沙箱未启用（要 ENABLE_SANDBOX=true）")
    check_code(code)
    workdir = _workdir()
    started = time.monotonic()
    try:
        data_path = _dataset_copy(dataset_id, workdir) if dataset_id else None
        outcome = exec.run_blocking(_run_process, workdir, code, data_path, int(timeout_s), int(max_output))
    finally:
        # 工作目录里有用户代码、数据集副本与输出，无论成败都当场删掉
        shutil.rmtree(workdir, ignore_errors=True)
    return {**outcome, "duration_ms": int((time.monotonic() - started) * 1000)}


# ponytail: 子进程隔离非容器隔离，公网多租户必须换容器
