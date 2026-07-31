from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "ADMIN"
    USER = "USER"
    SUPER_ADMIN = "SUPER_ADMIN"
    # Anonymous read-only visitor ("View as Guest"). Never stored on a real
    # user row — synthesised per-request by get_current_user_or_guest.
    GUEST = "GUEST"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"



class DeviceStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    MAINTENANCE = "maintenance"
    REPAIR = "repair"
    DISPOSED = "disposed"


class ResearcherRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

