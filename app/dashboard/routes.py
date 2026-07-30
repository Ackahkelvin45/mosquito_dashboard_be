from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPBearer
from fastapi import status
from sqlalchemy.orm import Session
from typing import Optional, Literal
from datetime import datetime

from app.core.database import get_db
from utils.protected_route import get_current_user
from app.service.dashboard_service import DashboardService
from app.dashboard.schema import DashboardResponse
from app.authentication.schema import UserResponse
from app.core.security.permissions import visible_cluster_ids


security = HTTPBearer()

router = APIRouter(tags=["dashboard"])

_GROUP_BY_DESC = (
    "Rolling time window: "
    "day = last 24 h | "
    "month = last 30 days | "
    "year = last 360 days (12 × 30-day buckets)"
)


@router.get(
    "",
    status_code=status.HTTP_200_OK,
    response_model=DashboardResponse,
    summary="Get all dashboard data",
    description="""
Returns all dashboard data in one call. **Totals and the bar chart each have their own
independent `group_by` filter**, so you can mix windows freely.

### group_by options
| Value | Window | Chart bucket |
|---|---|---|
| `day` | last 24 hours | 1-hour buckets |
| `month` *(default)* | last 30 days | 1-day buckets |
| `year` | last 360 days | 30-day buckets |

### Examples
```
# Totals from last year, chart for last month
GET /dashboard?totals_group_by=year&chart_group_by=month

# Both over the last day, filtered to a region
GET /dashboard?totals_group_by=day&chart_group_by=day&region=accra

# Zoom the chart into the last day while keeping yearly totals
GET /dashboard?totals_group_by=year&chart_group_by=day&cluster_id=2

# Custom date range — overrides every group_by; all sections share the window
# and echo group_by="custom" (buckets auto-scale to the span)
GET /dashboard?start_date=2026-01-01T00:00:00&end_date=2026-03-31T23:59:59
```
""",
)
def get_dashboard(
    session: Session = Depends(get_db),
    totals_group_by: Literal["day", "month", "year"] = Query(
        default="month",
        description=f"Window for summary card totals. {_GROUP_BY_DESC}",
    ),
    chart_group_by: Literal["day", "month", "year"] = Query(
        default="month",
        description=f"Window + bucket granularity for the bar chart. {_GROUP_BY_DESC}",
    ),
    gender_group_by: Literal["day", "month", "year"] = Query(
        default="month",
        description=f"Window for gender distribution pie chart. {_GROUP_BY_DESC}",
    ),
    region_group_by: Literal["day", "month", "year"] = Query(
        default="month",
        description=f"Window for mosquito count by region chart. {_GROUP_BY_DESC}",
    ),
    sensor_status_group_by: Literal["day", "month", "year"] = Query(
        default="month",
        description=f"Window + bucket granularity for the sensor status chart. {_GROUP_BY_DESC}",
    ),
    breakdown_group_by: Literal["day", "month", "year"] = Query(
        default="month",
        description=f"Window for mosquito breakdown. {_GROUP_BY_DESC}",
    ),
    correlation_group_by: Literal["day", "month", "year"] = Query(
        default="month",
        description=f"Window + bucket granularity for the mosquito vs temperature/humidity correlation chart. {_GROUP_BY_DESC}",
    ),
    genus_heatmap_group_by: Literal["day", "month", "year"] = Query(
        default="month",
        description=f"Window + bucket granularity for the genus distribution heatmap. {_GROUP_BY_DESC}",
    ),
    start_date: Optional[datetime] = Query(
        default=None,
        description="Custom window start (ISO 8601). Must be paired with end_date; overrides every group_by.",
    ),
    end_date: Optional[datetime] = Query(
        default=None,
        description="Custom window end (ISO 8601). Must be paired with start_date; overrides every group_by.",
    ),
    region: Optional[str] = Query(
        default=None,
        description="Filter devices by region (case-insensitive partial match).",
    ),
    cluster_id: Optional[int] = Query(
        default=None,
        description="Filter devices belonging to this cluster.",
    ),
    device_id: Optional[int] = Query(
        default=None,
        description="Scope the entire dashboard to a single device.",
    ),
    current_user: UserResponse = Depends(get_current_user),
):
    if (start_date is None) != (end_date is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_date and end_date must be provided together.",
        )
    if start_date is not None and end_date is not None and start_date >= end_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_date must be before end_date.",
        )
    try:
        # Restrict the whole dashboard to the caller's visible clusters
        # (None for a super admin = every cluster).
        allowed = visible_cluster_ids(session, current_user)
        return DashboardService(session).get_dashboard(
            totals_group_by=totals_group_by,
            chart_group_by=chart_group_by,
            gender_group_by=gender_group_by,
            region_group_by=region_group_by,
            sensor_status_group_by=sensor_status_group_by,
            breakdown_group_by=breakdown_group_by,
            correlation_group_by=correlation_group_by,
            genus_heatmap_group_by=genus_heatmap_group_by,
            region=region,
            cluster_id=cluster_id,
            device_id=device_id,
            allowed_cluster_ids=allowed,
            start_date=start_date,
            end_date=end_date,
        )
    except Exception as e:
        raise e
