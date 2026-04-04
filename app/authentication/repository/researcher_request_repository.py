from app.crud.base import BaseRepository
from app.authentication.models import ResearcherRequest,User
from app.authentication.schema import ResearcherRequestCreate, UpdateResearcherRequest
from datetime import datetime, timezone
from fastapi import HTTPException
from app.authentication.enums import ResearcherRequestStatus
from app.device.enums import Status as ClusterStatus
from app.device.models import DeviceCluster
import secrets
import re
from sqlalchemy.orm import joinedload



class ResearcherRequestRepository(BaseRepository[ResearcherRequest]):
    model = ResearcherRequest

    def get_researcher_requests(self) -> list[ResearcherRequest]:
        return (
            self.session.query(ResearcherRequest)
            .join(ResearcherRequest.user)
            .options(joinedload(ResearcherRequest.user))
            .all()
        )

    def _generate_cluster_password(self) -> str:
        # ~16-20 chars, URL-safe.
        return secrets.token_urlsafe(12)

    def _generate_cluster_name(self, user: User) -> str:
        base = f"{user.first_name}-{user.last_name}-{user.id}".strip().lower()
        base = re.sub(r"[^a-z0-9\\-]+", "-", base).strip("-")
        suffix = secrets.token_hex(4)
        name = f"{base}-{suffix}" if base else f"cluster-{user.id}-{suffix}"
        return name[:100]

    def _ensure_cluster_for_request(self, request: ResearcherRequest) -> DeviceCluster:
        user = self.session.query(User).filter(User.id == request.user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail=f"User with id {request.user_id} not found")

        if request.cluster_id is not None:
            cluster = self.session.query(DeviceCluster).filter(DeviceCluster.id == request.cluster_id).first()
            if not cluster:
                raise HTTPException(status_code=404, detail=f"Cluster with id {request.cluster_id} not found")
        else:
            cluster = DeviceCluster(
                name=self._generate_cluster_name(user),
                description=f"Cluster for {user.full_name}",
                password=self._generate_cluster_password(),
                public=False,
                status=ClusterStatus.APPROVED,
                cluster_admins=[user],
            )
            self.session.add(cluster)
            self.session.flush()  # get cluster.id
            request.cluster_id = cluster.id

        if user not in cluster.cluster_admins:
            cluster.cluster_admins.append(user)
        if not cluster.password:
            cluster.password = self._generate_cluster_password()

        return cluster

    def create_researcher_request(self, request_data: ResearcherRequestCreate) -> ResearcherRequest:

        if  self.session.query(ResearcherRequest).filter(ResearcherRequest.user_id == request_data.user_id).first():
            raise HTTPException(status_code=400, detail="User already has a pending researcher request")
        if self.session.query(User).filter(User.id == request_data.user_id).first() is None:
            raise HTTPException(status_code=404, detail=f"User with id {request_data.user_id} not found")
        if request_data.cluster_id is not None:
            if self.session.query(DeviceCluster).filter(DeviceCluster.id == request_data.cluster_id).first() is None:
                raise HTTPException(status_code=404, detail=f"Cluster with id {request_data.cluster_id} not found")
        
        data = request_data.model_dump(exclude_none=True)
        new_request = ResearcherRequest(**data)
        self.session.add(new_request)
        self.session.commit()
        self.session.refresh(new_request)
        return new_request

    def update_researcher_request_status(self, request_id: int, status: ResearcherRequestStatus | str) -> ResearcherRequest:
        request = self.get_by_id(request_id)

        if not request:
            raise HTTPException(status_code=404, detail=f"Researcher request with id {request_id} not found")

        try:
            raw_status = status.value if isinstance(status, ResearcherRequestStatus) else str(status)
            normalized_status = raw_status.strip().lower()
            if normalized_status == "declined":
                normalized_status = "rejected"
            status_enum = status if isinstance(status, ResearcherRequestStatus) else ResearcherRequestStatus(normalized_status)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid status value")

        if status_enum == ResearcherRequestStatus.APPROVED:
            self._ensure_cluster_for_request(request)

        request.status = status_enum
        self.session.commit()
        self.session.refresh(request)

        return request

    def update_reseacher_request(self, request_id: int, request_data: UpdateResearcherRequest) -> ResearcherRequest:
        request = self.get_by_id(request_id)

        if not request:
            raise HTTPException(status_code=404, detail=f"Researcher request with id {request_id} not found")

        if request_data.status is not None:
            try:
                normalized_status = request_data.status.strip().lower()
                if normalized_status == "declined":
                    normalized_status = "rejected"
                status_enum = ResearcherRequestStatus(normalized_status)
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid status value")

            if status_enum == ResearcherRequestStatus.APPROVED:
                self._ensure_cluster_for_request(request)

            request.status = status_enum

        if request_data.cluster_id is not None:
            if self.session.query(DeviceCluster).filter(DeviceCluster.id == request_data.cluster_id).first() is None:
                raise HTTPException(status_code=404, detail=f"Cluster with id {request_data.cluster_id} not found")
            request.cluster_id = request_data.cluster_id
        self.session.commit()
        self.session.refresh(request)

        return request


    def get_researchers_requests(self) -> list[ResearcherRequest]:
        # Backwards-compatible alias for earlier naming
        return self.get_researcher_requests()
    
    def get_researcher_request_by_id(self, request_id: int) -> ResearcherRequest:
        request = self.get_by_id(request_id)

        if not request:
            raise HTTPException(status_code=404, detail=f"Researcher request with id {request_id} not found")

        return request
        
    
