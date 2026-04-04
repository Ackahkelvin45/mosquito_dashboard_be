from sqlalchemy.orm import Session
from typing import Optional, List
from app.crud.base import BaseRepository
from app.device.models import Device, DeviceCluster
from app.device.schema import DeviceClusterResponse, DeviceClusterCreate, DeviceClusterUpdate
from datetime import datetime, timezone
from fastapi import HTTPException
from app.authentication.models import User


class DeviceClusterRepository(BaseRepository[DeviceCluster]):
    model = DeviceCluster

    def create_cluster(self, cluster_data: DeviceClusterCreate) -> DeviceCluster:
        data = cluster_data.model_dump(exclude_none=True)

        if "cluster_admins" in data:
            admin_ids = data.pop("cluster_admins")
            admins = self.session.query(User).filter(User.id.in_(admin_ids)).all()
            if len(admins) != len(admin_ids):
                raise HTTPException(status_code=404, detail="One or more cluster admins not found")
            data["cluster_admins"] = admins

        new_cluster = DeviceCluster(**data)
        self.session.add(new_cluster)
        self.session.commit()
        self.session.refresh(new_cluster)

        return new_cluster

    def update_cluster(self, cluster_id: int, cluster_data: DeviceClusterUpdate) -> DeviceCluster:
        cluster = self.get_by_id(cluster_id)

        if not cluster:
            raise HTTPException(status_code=404, detail=f"Cluster with id {cluster_id} not found")

        update_data = cluster_data.model_dump(exclude_none=True)

        if "cluster_admins" in update_data:
            admin_ids = update_data.pop("cluster_admins")
            admins = self.session.query(User).filter(User.id.in_(admin_ids)).all()
            if len(admins) != len(admin_ids):
                raise HTTPException(status_code=404, detail="One or more cluster admins not found")
            update_data["cluster_admins"] = admins

        for key, value in update_data.items():
            setattr(cluster, key, value)

        self.session.commit()
        self.session.refresh(cluster)

        return cluster

    def get_by_id(self, cluster_id: int) -> Optional[DeviceCluster]:
        return (
            self.session.query(DeviceCluster)
            .filter(DeviceCluster.id == cluster_id)
            .first()
        )

    def get_by_uuid(self, cluster_uuid: str) -> Optional[DeviceCluster]:
        return (
            self.session.query(DeviceCluster)
            .filter(DeviceCluster.cluster_uuid == cluster_uuid)
            .first()
        )

    def get_all(self) -> List[DeviceCluster]:
        return self.session.query(DeviceCluster).all()

    def delete_cluster(self, cluster_id: int) -> None:
        cluster = self.get_by_id(cluster_id)

        if not cluster:
            raise HTTPException(status_code=404, detail=f"Cluster with id {cluster_id} not found")

        self.session.delete(cluster)
        self.session.commit()

    def add_admin(self, cluster_id: int, user_id: int) -> DeviceCluster:
        cluster = self.get_by_id(cluster_id)
        if not cluster:
            raise HTTPException(status_code=404, detail=f"Cluster with id {cluster_id} not found")

        user = self.session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail=f"User with id {user_id} not found")

        if user in cluster.cluster_admins:
            raise HTTPException(status_code=400, detail="User is already an admin of this cluster")

        cluster.cluster_admins.append(user)
        self.session.commit()
        self.session.refresh(cluster)

        return cluster

    def remove_admin(self, cluster_id: int, user_id: int) -> DeviceCluster:
        cluster = self.get_by_id(cluster_id)
        if not cluster:
            raise HTTPException(status_code=404, detail=f"Cluster with id {cluster_id} not found")

        user = self.session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail=f"User with id {user_id} not found")

        if user not in cluster.cluster_admins:
            raise HTTPException(status_code=400, detail="User is not an admin of this cluster")

        cluster.cluster_admins.remove(user)
        self.session.commit()
        self.session.refresh(cluster)

        return cluster
    
    def change_cluster_status(self, cluster_id: int, status: str) -> DeviceCluster:
        cluster = self.get_by_id(cluster_id)
        if not cluster:
            raise HTTPException(status_code=404, detail=f"Cluster with id {cluster_id} not found")

        if status not in ["approved", "rejected", "pending"]:
            raise HTTPException(status_code=400, detail="Invalid status value")

        cluster.status = status
        self.session.commit()
        self.session.refresh(cluster)

        return cluster