# 文件：app/store.py
# 作用：SQLite 元数据层：建表与读写，所有元数据只经这里落盘
# 阶段：P0 骨架与契约冻结（扩展阶段在此追加新表；A 类补丁加 tasks 写入与数据集删除）
#       P6 加 skills 表；P7 加 kb_docs 与检索索引 kb_fts
# 依赖：标准库 sqlite3、app/config.py
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing

from app import config

logger = logging.getLogger(__name__)

DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS datasets (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, source_id TEXT, source_path TEXT,
        table_name TEXT NOT NULL, rows INTEGER, cols INTEGER, profile_json TEXT,
        clean_log TEXT, table_version INTEGER DEFAULT 1, owner TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY, dataset_id TEXT, session_id TEXT, kind TEXT, question TEXT,
        sql TEXT, status TEXT, degraded_json TEXT, error TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS agent_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, n INTEGER, tool TEXT,
        args_json TEXT, ok INTEGER, ms INTEGER, rows INTEGER, error TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS trace_spans (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, name TEXT, model_id TEXT,
        tokens_in INTEGER, tokens_out INTEGER, cost REAL, ms INTEGER, ok INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS capability_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, capability TEXT, event TEXT, detail TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS skills (
        id TEXT PRIMARY KEY, slug TEXT UNIQUE, path TEXT, description TEXT,
        kind TEXT, enabled INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS kb_docs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, title TEXT, chunk_no INTEGER,
        content TEXT, source_type TEXT, vector BLOB, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    # 检索索引与 kb_docs 分开存：trigram 分词是按子串命中中文的唯一选择（unicode61 搜不到连续中文）
    """CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(content, tokenize='trigram')""",
)


def connect() -> sqlite3.Connection:
    """打开元数据库连接，调用方负责关闭。"""
    config.ensure_dirs()
    conn = sqlite3.connect(config.SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables() -> None:
    """建表，幂等；扩展阶段新增表时在这里追加一条 DDL。"""
    with closing(connect()) as conn:
        for stmt in DDL:
            conn.execute(stmt)
        conn.commit()


def table_names() -> list[str]:
    """当前库里的表名，供自检与测试使用。"""
    with closing(connect()) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
    return [row["name"] for row in rows]


def insert_dataset(dataset: dict) -> None:
    """写入一个数据集的元数据。"""
    ensure_tables()
    fields = (
        "id",
        "name",
        "source_id",
        "source_path",
        "table_name",
        "rows",
        "cols",
        "profile_json",
        "clean_log",
        "table_version",
        "owner",
    )
    values = [dataset.get(field) for field in fields]
    with closing(connect()) as conn:
        conn.execute(
            f"INSERT INTO datasets ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
            values,
        )
        conn.commit()


def list_datasets() -> list[dict]:
    """返回所有数据集的简要元数据。"""
    ensure_tables()
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT id, name, table_name, rows, cols, table_version, created_at "
            "FROM datasets ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_dataset(dataset_id: str) -> dict | None:
    """返回指定数据集及其画像，不存在时返回 None。"""
    ensure_tables()
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["profile"] = json.loads(result.pop("profile_json") or "{}")
    result["clean_log"] = json.loads(result["clean_log"] or "[]")
    return result


def insert_task(task: dict) -> None:
    """写一条提问任务（K-005）；status 由 degraded 是否有内容决定，写失败只记日志，不拖垮提问。"""
    try:
        ensure_tables()
        degraded = [str(item) for item in task.get("degraded") or []]
        with closing(connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tasks "
                "(id, dataset_id, session_id, kind, question, sql, status, degraded_json, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task.get("id"),
                    task.get("dataset_id"),
                    task.get("session_id") or "",
                    task.get("kind") or "ask",
                    task.get("question") or "",
                    task.get("sql") or "",
                    "degraded" if degraded else "ok",
                    json.dumps(degraded, ensure_ascii=False),
                    task.get("error") or "",
                ),
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.warning("任务写入失败(%s)：%s", task.get("id"), exc)


def delete_dataset(dataset_id: str) -> bool:
    """删掉数据集元数据及其任务与步骤流水（K-015），返回是否删到了行。"""
    ensure_tables()
    with closing(connect()) as conn:
        conn.execute(
            "DELETE FROM agent_steps WHERE task_id IN (SELECT id FROM tasks WHERE dataset_id = ?)",
            (dataset_id,),
        )
        conn.execute("DELETE FROM tasks WHERE dataset_id = ?", (dataset_id,))
        cursor = conn.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
        conn.commit()
    return cursor.rowcount > 0


def insert_agent_step(task_id: str, step: dict) -> None:
    """写一条 agent 步骤流水；args_json 存的是参数摘要（设计的 args_digest），完整参数落库没意义。"""
    ensure_tables()
    with closing(connect()) as conn:
        conn.execute(
            "INSERT INTO agent_steps (task_id, n, tool, args_json, ok, ms, rows, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                step.get("n"),
                step.get("tool"),
                step.get("args_digest"),
                int(bool(step.get("ok"))),
                step.get("ms"),
                step.get("rows"),
                step.get("error", ""),
            ),
        )
        conn.commit()


def log_capability(capability: str, event: str, detail: str = "") -> None:
    """写一条能力流水（越权调用、能力上下线等）；这是诊断信息，写失败不能拖垮主流程。"""
    try:
        ensure_tables()
        with closing(connect()) as conn:
            conn.execute(
                "INSERT INTO capability_log (capability, event, detail) VALUES (?, ?, ?)",
                (capability, event, detail),
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.warning("能力流水写入失败(%s/%s)：%s", capability, event, exc)


def upsert_skill(skill: dict) -> None:
    """按 slug 写入或更新一条技能；同一目录重复导入只更新，不产生重复行。"""
    ensure_tables()
    with closing(connect()) as conn:
        conn.execute(
            "INSERT INTO skills (id, slug, path, description, kind, enabled) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET path = excluded.path, description = excluded.description, "
            "kind = excluded.kind, enabled = excluded.enabled",
            (
                skill.get("id") or f"sk_{skill.get('slug')}",
                skill.get("slug"),
                skill.get("path"),
                skill.get("description") or "",
                skill.get("kind") or "prompt",
                int(bool(skill.get("enabled", 1))),
            ),
        )
        conn.commit()


def list_skills() -> list[dict]:
    """返回已导入的技能，按 slug 排序。"""
    ensure_tables()
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT id, slug, path, description, kind, enabled, created_at FROM skills ORDER BY slug"
        ).fetchall()
    return [dict(row) for row in rows]


def get_skill(slug: str) -> dict | None:
    """按 slug 取一条技能，没有时返回 None。"""
    ensure_tables()
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM skills WHERE slug = ?", (slug,)).fetchone()
    return dict(row) if row else None


def replace_kb_doc(doc: dict, chunks: list[str]) -> int:
    """替换一份文档的全部分块（重复导入只更新、不产生重复块），返回写入的块数。"""
    ensure_tables()
    with closing(connect()) as conn:
        old = [row["id"] for row in conn.execute("SELECT id FROM kb_docs WHERE path = ?", (doc.get("path"),))]
        for row_id in old:
            conn.execute("DELETE FROM kb_fts WHERE rowid = ?", (row_id,))
        conn.execute("DELETE FROM kb_docs WHERE path = ?", (doc.get("path"),))
        for index, text in enumerate(chunks, start=1):
            cursor = conn.execute(
                "INSERT INTO kb_docs (path, title, chunk_no, content, source_type) VALUES (?, ?, ?, ?, ?)",
                (doc.get("path"), doc.get("title"), index, text, doc.get("source_type")),
            )
            # 正文在两处各存一份：kb_docs 给人看与取正文，kb_fts 只管检索
            conn.execute("INSERT INTO kb_fts (rowid, content) VALUES (?, ?)", (cursor.lastrowid, text))
        conn.commit()
    return len(chunks)


def search_kb(query: str, limit: int = 5) -> list[dict]:
    """FTS5 检索（bm25 排序）；查询词短于 3 个字符时退回 LIKE（trigram 建不出那么短的词）。"""
    ensure_tables()
    with closing(connect()) as conn:
        if len(query) >= 3:
            # 整串当短语查：输入里的 FTS 语法字符（*、"、AND/OR）不再是语法，少了注入面
            phrase = '"' + query.replace('"', '""') + '"'
            rows = conn.execute(
                "SELECT d.path, d.title, d.chunk_no, d.content, bm25(kb_fts) AS score "
                "FROM kb_fts JOIN kb_docs d ON d.id = kb_fts.rowid "
                "WHERE kb_fts MATCH ? ORDER BY bm25(kb_fts) LIMIT ?",
                (phrase, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT d.path, d.title, d.chunk_no, d.content, 0.0 AS score "
                "FROM kb_docs d WHERE d.content LIKE ? ORDER BY d.id LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
    return [dict(row) for row in rows]


def count_kb_chunks() -> int:
    """已入库的知识库块数，供自检与测试使用。"""
    ensure_tables()
    with closing(connect()) as conn:
        row = conn.execute("SELECT count(*) AS n FROM kb_docs").fetchone()
    return int(row["n"])
