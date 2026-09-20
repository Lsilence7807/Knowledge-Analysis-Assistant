# 文件：backend/app/models/base.py
# 作用：ORM 基类（§4.4 的表都继承它）；引擎与会话在 core/db.py
# 阶段：F6 数据层换 SQLAlchemy + Alembic
# 依赖：sqlalchemy
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """全部 ORM 表的基类。"""
