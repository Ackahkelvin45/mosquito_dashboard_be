from fastapi import APIRouter, Depends, Query
from fastapi.security import HTTPBearer
from fastapi import status
from sqlalchemy.orm import Session
from typing import Optional, Literal

from app.core.database import get_db
from app.service.dashboard_service import DashboardService
from app.dashboard.schema import DashboardResponse


security = HTTPBearer()

router = APIRouter(tags=["dashboard"])

_GROUP_BY_DESC = (
    "Rolling time window: "
    "hour = last 60 min | "
    "day = last 24 h | "
    "week = last 7 days | "
    "month = last 30 days"
)


@router.get(
    "",
    status_code=status.HTTP_200_OK,
    response_model=DashboardResponse,
    dependencies=[Depends(security)],
    summary="Get all dashboard data",
    description="""
Returns all dashboard data in one call. **Totals and the bar chart each have their own
independent `group_by` filter**, so you can mix windows freely.

### group_by options
| Value | Window | Chart bucket |
|---|---|---|
| `hour` | last 60 minutes | 1-minute buckets |
| `day` | last 24 hours | 1-hour buckets |
| `week` *(default)* | last 7 days | 1-day buckets |
| `month` | last 30 days | 1-day buckets |

### Examples
```
# Totals from last month, chart for last week
GET /dashboard?totals_group_by=month&chart_group_by=week

# Both over the last day, filtered to a region
GET /dashboard?totals_group_by=day&chart_group_by=day&region=accra

# Zoom the chart into the last hour while keeping monthly totals
GET /dashboard?totals_group_by=month&chart_group_by=hour&cluster_id=2
```
""",
)
def get_dashboard(
    session: Session = Depends(get_db),
    totals_group_by: Literal["hour", "day", "week", "month"] = Query(
        default="week",
        description=f"Window for summary card totals. {_GROUP_BY_DESC}",
    ),
    chart_group_by: Literal["hour", "day", "week", "month"] = Query(
        default="week",
        description=f"Window + bucket granularity for the bar chart. {_GROUP_BY_DESC}",
    ),
    gender_group_by: Literal["hour", "day", "week", "month"] = Query(
        default="week",
        description=f"Window for gender distribution pie chart. {_GROUP_BY_DESC}",
    ),
    region_group_by: Literal["hour", "day", "week", "month"] = Query(
        default="week",
        description=f"Window for mosquito count by region chart. {_GROUP_BY_DESC}",
    ),
    sensor_status_group_by: Literal["hour", "day", "week", "month"] = Query(
        default="week",
        description=f"Window + bucket granularity for the sensor status chart. {_GROUP_BY_DESC}",
    ),
    breakdown_group_by: Literal["hour", "day", "week", "month"] = Query(
        default="week",
        description=f"Window for mosquito breakdown. {_GROUP_BY_DESC}",
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
):
    try:
        return DashboardService(session).get_dashboard(
            totals_group_by=totals_group_by,
            chart_group_by=chart_group_by,
            gender_group_by=gender_group_by,
            region_group_by=region_group_by,
            sensor_status_group_by=sensor_status_group_by,
            breakdown_group_by=breakdown_group_by,
            region=region,
            cluster_id=cluster_id,
            device_id=device_id,
        )
    except Exception as e:
        raise e
