from typing import List, Optional
from datetime import datetime, timezone
import uuid


class GameRegulation:
    def __init__(
        self,
        id: Optional[str] = None,
        game_name: str = "",
        chapter_code: str = "",
        title: str = "",
        format: str = "",
        server: str = "",
        map_pool: Optional[List[str]] = None,
        rules: Optional[List[str]] = None,
        order: int = 0,
        is_active: bool = True,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ):
        now_iso = datetime.now(timezone.utc).isoformat()
        self.id = id or f"reg_{uuid.uuid4().hex[:10]}"
        self.game_name = game_name
        self.chapter_code = chapter_code
        self.title = title or game_name
        self.format = format
        self.server = server
        self.map_pool = map_pool or []
        self.rules = rules or []
        self.order = order
        self.is_active = is_active
        self.created_at = created_at or now_iso
        self.updated_at = updated_at or now_iso

    def to_mongo(self) -> dict:
        return {
            "_id": self.id,
            "id": self.id,
            "game_name": self.game_name,
            "chapter_code": self.chapter_code,
            "title": self.title,
            "format": self.format,
            "server": self.server,
            "map_pool": self.map_pool,
            "rules": self.rules,
            "order": self.order,
            "is_active": self.is_active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mongo(cls, doc: dict) -> "GameRegulation":
        doc_id = str(doc.get("_id", doc.get("id", "")))
        return cls(
            id=doc_id,
            game_name=doc.get("game_name", ""),
            chapter_code=doc.get("chapter_code", ""),
            title=doc.get("title", ""),
            format=doc.get("format", ""),
            server=doc.get("server", ""),
            map_pool=doc.get("map_pool", []),
            rules=doc.get("rules", []),
            order=doc.get("order", 0),
            is_active=doc.get("is_active", True),
            created_at=doc.get("created_at"),
            updated_at=doc.get("updated_at"),
        )


class RegulationTable:
    def __init__(
        self,
        id: Optional[str] = None,
        title: str = "",
        description: Optional[str] = None,
        columns: Optional[List[str]] = None,
        rows: Optional[List[List[str]]] = None,
        order: int = 0,
        is_active: bool = True,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ):
        now_iso = datetime.now(timezone.utc).isoformat()
        self.id = id or f"tbl_{uuid.uuid4().hex[:10]}"
        self.title = title
        self.description = description or ""
        self.columns = columns or []
        self.rows = rows or []
        self.order = order
        self.is_active = is_active
        self.created_at = created_at or now_iso
        self.updated_at = updated_at or now_iso

    def to_mongo(self) -> dict:
        return {
            "_id": self.id,
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "columns": self.columns,
            "rows": self.rows,
            "order": self.order,
            "is_active": self.is_active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mongo(cls, doc: dict) -> "RegulationTable":
        doc_id = str(doc.get("_id", doc.get("id", "")))
        return cls(
            id=doc_id,
            title=doc.get("title", ""),
            description=doc.get("description", ""),
            columns=doc.get("columns", []),
            rows=doc.get("rows", []),
            order=doc.get("order", 0),
            is_active=doc.get("is_active", True),
            created_at=doc.get("created_at"),
            updated_at=doc.get("updated_at"),
        )
