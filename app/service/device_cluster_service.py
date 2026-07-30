from fastapi import HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.device.repository.device_cluster_repository import DeviceClusterRepository
from app.device.schema import DeviceClusterCreate, DeviceClusterResponse, DeviceClusterUpdate
from app.core.pagination import Page, paginate
from app.notification.events import NotificationEvent, emit


class DeviceClusterService:
    def __init__(self, session: Session):
        self.session = session
        self.cluster_repository = DeviceClusterRepository(session)

    def create_cluster(self, cluster_data: DeviceClusterCreate) -> DeviceClusterResponse:
        cluster = self.cluster_repository.create_cluster(cluster_data)
        response = DeviceClusterResponse.model_validate(cluster)
        emit(self.session, NotificationEvent.CLUSTER_CREATED, cluster=cluster)
        return response

    def update_cluster(self, cluster_id: int, cluster_data: DeviceClusterUpdate) -> DeviceClusterResponse:
        cluster = self.cluster_repository.update_cluster(cluster_id, cluster_data)
        response = DeviceClusterResponse.model_validate(cluster)
        emit(self.session, NotificationEvent.CLUSTER_UPDATED, cluster=cluster)
        return response

    def get_clusters(self, page: int = 1, page_size: int = 20, allowed_cluster_ids=None) -> Page[DeviceClusterResponse]:
        clusters = self.cluster_repository.get_all()
        # A scoped caller only sees their own cluster plus public ones.
        if allowed_cluster_ids is not None:
            clusters = [c for c in clusters if c.id in allowed_cluster_ids]
        sliced, total, total_pages = paginate(clusters, page, page_size)
        return Page[DeviceClusterResponse](
            items=[DeviceClusterResponse.model_validate(cluster) for cluster in sliced],
            total=total, page=page, page_size=page_size, total_pages=total_pages,
        )

    def get_cluster_by_id(self, cluster_id: int) -> DeviceClusterResponse:
        cluster = self.cluster_repository.get_by_id(cluster_id)
        if not cluster:
            raise HTTPException(status_code=404, detail="Device cluster not found")
        return DeviceClusterResponse.model_validate(cluster)

    def delete_cluster(self, cluster_id: int) -> None:
        self.cluster_repository.delete_cluster(cluster_id)

    def add_admin_to_cluster(self, cluster_id: int, admin_id: int) -> DeviceClusterResponse:
        cluster = self.cluster_repository.add_admin(cluster_id, admin_id)
        return DeviceClusterResponse.model_validate(cluster)

    def remove_admin_from_cluster(self, cluster_id: int, admin_id: int) -> DeviceClusterResponse:
      
        cluster = self.cluster_repository.remove_admin(cluster_id, admin_id)
        return DeviceClusterResponse.model_validate(cluster)

    def change_cluster_status(self, cluster_id: int, status: str) -> DeviceClusterResponse:
        cluster = self.cluster_repository.change_cluster_status(cluster_id, status)
        return DeviceClusterResponse.model_validate(cluster)