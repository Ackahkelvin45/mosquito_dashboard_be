from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=100, description="Human-readable name for the key")
    description: Optional[str] = Field(None, max_length=255, description="What this key is used for")
    expires_in_days: Optional[int] = Field(
        None, ge=1, le=3650, description="Days until the key expires; omit for a non-expiring key"
    )


class ApiKeyResponse(BaseModel):
    """Key metadata — never contains the raw key or its hash."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: Optional[str] = None
    key_prefix: str
    user_id: int
    created_at: datetime
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    total_requests: int
    # Populated only in the super-admin ?all=true listing.
    owner_email: Optional[str] = None
    revoked_by_email: Optional[str] = None


class ApiKeyCreatedResponse(ApiKeyResponse):
    # The raw key — returned exactly once, at creation. Not recoverable later.
    api_key: str
