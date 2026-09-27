from typing import List, Optional
from pydantic import BaseModel, Field, ConfigDict


class GameRegulationCreate(BaseModel):
    game_name: str = Field(..., min_length=2, max_length=100)
    chapter_code: str = Field(..., min_length=2, max_length=30)
    title: str = Field(..., min_length=2, max_length=150)
    format: str = Field(..., min_length=2, max_length=200)
    server: str = Field(..., min_length=2, max_length=200)
    map_pool: List[str] = Field(default_factory=list)
    rules: List[str] = Field(default_factory=list)
    order: Optional[int] = 0
    is_active: Optional[bool] = True


class GameRegulationUpdate(BaseModel):
    game_name: Optional[str] = None
    chapter_code: Optional[str] = None
    title: Optional[str] = None
    format: Optional[str] = None
    server: Optional[str] = None
    map_pool: Optional[List[str]] = None
    rules: Optional[List[str]] = None
    order: Optional[int] = None
    is_active: Optional[bool] = None


class GameRegulationResponse(BaseModel):
    id: str
    game_name: str
    chapter_code: str
    title: str
    format: str
    server: str
    map_pool: List[str] = Field(default_factory=list)
    rules: List[str] = Field(default_factory=list)
    order: int = 0
    is_active: bool = True
    created_at: str
    updated_at: str
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class RegulationReorderRequest(BaseModel):
    ordered_ids: List[str] = Field(..., min_length=1)


class RegulationTableCreate(BaseModel):
    title: str = Field(..., min_length=2, max_length=150)
    description: Optional[str] = ""
    columns: List[str] = Field(..., min_length=1)
    rows: List[List[str]] = Field(default_factory=list)
    order: Optional[int] = 0
    is_active: Optional[bool] = True


class RegulationTableUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    columns: Optional[List[str]] = None
    rows: Optional[List[List[str]]] = None
    order: Optional[int] = None
    is_active: Optional[bool] = None


class RegulationTableResponse(BaseModel):
    id: str
    title: str
    description: Optional[str] = ""
    columns: List[str] = Field(default_factory=list)
    rows: List[List[str]] = Field(default_factory=list)
    order: int = 0
    is_active: bool = True
    created_at: str
    updated_at: str
    model_config = ConfigDict(from_attributes=True, extra="ignore")
