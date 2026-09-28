from typing import Optional, List, Any, Dict
from pydantic import BaseModel, Field

class ArchiveFilterRequest(BaseModel):
    status: Optional[str] = None
    statuses: Optional[List[str]] = None
    category: Optional[str] = None
    txn_type: Optional[str] = None
    payment_type: Optional[str] = None
    direction: Optional[str] = None  # "CREDIT", "DEBIT"
    action: Optional[str] = None
    operator: Optional[str] = None
    user_id: Optional[str] = None
    is_read: Optional[bool] = None
    date_preset: Optional[str] = None  # "older_7_days", "older_30_days", "older_90_days", "custom", "all"
    date_before: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    search: Optional[str] = None
    target_ids: Optional[List[str]] = None
    reason: Optional[str] = "Admin section cleanup"

class ArchivePreviewResponse(BaseModel):
    section: str
    matching_count: int
    filters_applied: Dict[str, Any]

class ArchiveExecutionResponse(BaseModel):
    success: bool
    section: str
    archived_count: int
    message: str

class UnarchiveResponse(BaseModel):
    success: bool
    section: str
    unarchived_count: int
    message: str

class BulkUnarchiveRequest(BaseModel):
    target_ids: List[str]
