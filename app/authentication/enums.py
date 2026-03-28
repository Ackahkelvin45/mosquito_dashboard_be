from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "admin"
    USER = "user"
    SUPER_ADMIN = "super_admin"
  

class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"



class DeviceStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    MAINTENANCE = "maintenance"
    REPAIR = "repair"
    DISPOSED = "disposed"

