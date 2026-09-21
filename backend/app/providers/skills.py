# 文件：backend/app/providers/skills.py
# 作用：技能库：导入项目内的技能目录（解析 SKILL.md frontmatter）→ 入库 → 列表；prompt 型取正文片段，script 型子进程执行
# 阶段：P6 Skill 导入与执行
# 依赖：标准库 os、re、subprocess、sys、pathlib、backend/app/core/config.py、backend/app/services/store.py
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from app.core import config, guardrail
from app.services import store

ENABLED = os.getenv("ENABLE_SKILLS", "false").lower() == "true"
TIMEOUT_S = int(os.getenv("SKILL_TIMEOUT_S", "20"))
DEFAULT_DIR = "skills"
ROOT_DIR = config.BASE_DIR
KINDS = ("prompt", "script")
MAX_OUTPUT_CHARS = 4000
MAX_ARGS = 8

# 本机手写的 SKILL.md 常是 CRLF，两端都容忍：只认「以 --- 包住的键值块」这一种形状
_FRONTMATTER = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)


class SkillError(RuntimeError):
    """技能导入或执行失败（路径越界、frontmatter 缺失、脚本超时）；信息可直接回给模型或用户。"""


def resolve_dir(path: str = "") -> Path:
    """把技能目录解析成项目内的绝对路径：相对路径按项目根算，越界一律拒绝。"""
    candidate = Path(path or DEFAULT_DIR)
    resolved = (candidate if candidate.is_absolute() else ROOT_DIR / candidate).resolve()
    # §7 路径边界：技能目录只能落在项目内，`../../` 这类逃逸挡在导入之前
    if resolved != ROOT_DIR and ROOT_DIR not in resolved.parents:
        raise SkillError(f"技能目录必须在项目内，已拒绝：{path}")
    return resolved


def parse_frontmatter(text: str) -> dict:
    """解析 SKILL.md 开头的 frontmatter（扁平 key: value），没有 frontmatter 直接抛错。"""
    match = _FRONTMATTER.match(text)
    if match is None:
        raise SkillError("SKILL.md 缺 frontmatter：文件必须以 --- 包住的键值块开头")
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip().strip("\"'")
    return meta


def skill_body(text: str) -> str:
    """取 frontmatter 之后的正文，即 prompt 型技能要送进上下文的那段。"""
    match = _FRONTMATTER.match(text)
    return (text[match.end() :] if match else text).strip()


def _entry_path(skill_dir: Path, entry: str) -> Path:
    """把声明的入口解析到技能目录内；指向目录外或文件不存在都拒绝。"""
    resolved = (skill_dir / entry).resolve()
    if skill_dir.resolve() not in resolved.parents:
        raise SkillError(f"入口脚本必须在技能目录内，已拒绝：{entry}")
    if not resolved.is_file():
        raise SkillError(f"入口脚本不存在：{entry}")
    return resolved


def _skill_file(row: dict) -> Path:
    """由库里的相对路径还原技能文件位置，并再校验一次没跑到项目外。"""
    path = (ROOT_DIR.resolve() / str(row.get("path") or "")).resolve()
    if ROOT_DIR.resolve() not in path.parents:
        raise SkillError(f"技能文件在项目外，已拒绝：{row.get('path')}")
    if not path.is_file():
        raise SkillError(f"技能文件不在了，重新导入试试：{row.get('path')}")
    return path


def read_skill(skill_dir: Path) -> dict:
    """读一个技能目录的 SKILL.md，返回可入库的一行；kind 非法或 script 型没声明入口都抛错。"""
    if not skill_dir.is_dir():
        raise SkillError(f"技能目录不存在：{skill_dir}")
    path = skill_dir / "SKILL.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillError(f"SKILL.md 读不出来：{exc}") from exc
    meta = parse_frontmatter(text)
    kind = (meta.get("kind") or "prompt").lower()
    if kind not in KINDS:
        raise SkillError(f"kind 只能是 {'/'.join(KINDS)}，收到：{kind}")
    if kind == "script":
        entry = meta.get("entry") or ""
        if not entry:
            raise SkillError("script 型技能必须在 frontmatter 里声明 entry（入口脚本）")
        _entry_path(skill_dir, entry)
    slug = (meta.get("name") or skill_dir.name).strip()
    try:
        relative = path.resolve().relative_to(ROOT_DIR.resolve()).as_posix()
    except ValueError as exc:
        raise SkillError(f"技能目录必须在项目内，已拒绝：{skill_dir}") from exc
    return {
        "id": f"sk_{slug}",
        "slug": slug,
        "path": relative,
        "description": meta.get("description") or "",
        "kind": kind,
        "enabled": 0 if (meta.get("enabled") or "true").lower() in ("false", "0", "no") else 1,
    }


def import_dir(path: str = "") -> dict:
    """导入一个技能目录下的全部技能（默认 skills/）：逐个解析入库，坏的单独跳过、不拖垮整批。"""
    directory = resolve_dir(path)
    if not directory.is_dir():
        raise SkillError(f"技能目录不存在：{path or DEFAULT_DIR}")
    imported: list[dict] = []
    skipped: list[dict] = []
    for child in sorted(directory.iterdir()):
        # 只有带 SKILL.md 的子目录算技能，技能目录里的脚本与说明文件不该被判成坏技能
        if not child.is_dir() or not (child / "SKILL.md").is_file():
            continue
        try:
            row = read_skill(child)
        except SkillError as exc:
            skipped.append({"path": child.name, "error": str(exc)})
            continue
        store.upsert_skill(row)
        imported.append({key: row[key] for key in ("slug", "kind", "description", "enabled")})
    return {"dir": directory.as_posix(), "imported": imported, "skipped": skipped}


def list_skills() -> list[dict]:
    """已导入技能的清单（slug、类型、说明、是否启用、SKILL.md 相对路径）。"""
    return store.list_skills()


def run_script(skill_dir: Path, entry: str, args: list | None = None) -> dict:
    """子进程跑技能入口脚本：工作目录锁在技能目录内，超时直接杀，输出截断后回给调用方。"""
    script = _entry_path(skill_dir, entry)
    argv = [str(item) for item in (args or [])][:MAX_ARGS]
    # ponytail: 子进程继承服务进程环境（含密钥）；技能脚本按「项目内可信代码」对待，真正隔离是 P15 沙箱的事
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run(
            [sys.executable, str(script), *argv],
            cwd=skill_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run 超时时会先 kill 子进程再抛错，这里只负责把原因说清楚
        raise SkillError(f"技能脚本超时（>{TIMEOUT_S}s）已被终止：{entry}") from exc
    return {
        "returncode": proc.returncode,
        "output": (proc.stdout or "")[-MAX_OUTPUT_CHARS:],
        "stderr": ((proc.stderr or "")[-MAX_OUTPUT_CHARS:] if proc.returncode else ""),
    }


def use_skill(slug: str, args: list | None = None) -> dict:
    """取用一个技能：prompt 型回正文片段（由调用方送进上下文），script 型跑入口脚本并回输出。"""
    row = store.get_skill(slug)
    if row is None:
        raise SkillError(f"没有这个技能：{slug}；先用 list_skills 看有哪些")
    if not row["enabled"]:
        raise SkillError(f"技能 {slug} 已被禁用，本次不能用")
    path = _skill_file(row)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillError(f"SKILL.md 读不出来：{exc}") from exc
    if row["kind"] != "script":
        body = skill_body(text)
        # K-031：技能正文进上下文前统一过注入防护（剥指令行 + 命中留痕），不再原样塞给模型
        body = guardrail.guard(body, f"技能 {slug}")
        # 正文可能很长（实测有 2 万字符的），与脚本输出同一个上限：截断并标记，别让一次 use_skill 吃掉上下文
        kept = body[:MAX_OUTPUT_CHARS]
        return {
            "slug": slug,
            "kind": row["kind"],
            "description": row["description"],
            "content": kept,
            "truncated": len(kept) < len(body),
        }
    # 入口以文件为准（改 frontmatter 不用重新导入），但仍锁在技能目录内
    entry = parse_frontmatter(text).get("entry") or ""
    if not entry:
        raise SkillError(f"技能 {slug} 没有声明 entry，不能执行")
    return {"slug": slug, "kind": "script", "description": row["description"], **run_script(path.parent, entry, args)}
