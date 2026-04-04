from fastapi import HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.device.repository.device_cluster_repository import DeviceClusterRepository
from app.device.schema import DeviceClusterCreate, DeviceClusterResponse, DeviceClusterUpdate


class DeviceClusterService:
    def __init__(self, session: Session):
        self.cluster_repository = DeviceClusterRepository(session)

    def create_cluster(self, cluster_data: DeviceClusterCreate) -> DeviceClusterResponse:
        cluster = self.cluster_repository.create_cluster(cluster_data)
        return DeviceClusterResponse.model_validate(cluster)

    def update_cluster(self, cluster_id: int, cluster_data: DeviceClusterUpdate) -> DeviceClusterResponse:
        cluster = self.cluster_repository.update_cluster(cluster_id, cluster_data)
        return DeviceClusterResponse.model_validate(cluster)

    def get_clusters(self) -> List[DeviceClusterResponse]:
        clusters = self.cluster_repository.get_all()
        return [DeviceClusterResponse.model_validate(cluster) for cluster in clusters]

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