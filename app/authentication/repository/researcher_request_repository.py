from app.crud.base import BaseRepository
from app.authentication.models import ResearcherRequest,User
from app.authentication.schema import ResearcherRequestCreate, UpdateResearcherRequest
from datetime import datetime, timezone
from fastapi import HTTPException
from app.authentication.enums import ResearcherRequestStatus
from app.device.enums import Status as ClusterStatus
from app.device.models import DeviceCluster
import logging
import secrets
import re
from sqlalchemy.orm import joinedload

logger = logging.getLogger(__name__)



class ResearcherRequestRepository(BaseRepository[ResearcherRequest]):
    model = ResearcherRequest
    # Set by the status updates: cluster id whose membership a rejection
    # revoked (None otherwise), so the route can put it in the audit row.
    revoked_cluster_id: int | None = None

    def get_researcher_requests(self, status: ResearcherRequestStatus | None = None,
                                search: str | None = None) -> list[ResearcherRequest]:
        query = (
            self.session.query(ResearcherRequest)
            .join(ResearcherRequest.user)
            .options(joinedload(ResearcherRequest.user))
        )
        if status is not None:
            query = query.filter(ResearcherRequest.status == status)
        if search:
            like = f"%{search}%"
            query = query.filter(User.first_name.ilike(like) | User.last_name.ilike(like) | User.email.ilike(like))
        return query.order_by(ResearcherRequest.id).all()

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
                public=False,
                status=ClusterStatus.APPROVED,
                cluster_admins=[user],
            )
            self.session.add(cluster)
            self.session.flush()  # get cluster.id
            request.cluster_id = cluster.id

        if user not in cluster.cluster_admins:
            cluster.cluster_admins.append(user)

        # users.cluster_id is the membership that data scoping and alert
        # fan-out read (cluster_admins is not). Fill it only when empty: a user
        # already in another cluster is never moved by an approval — that is
        # an explicit super-admin action (PATCH /auth/users/{id}).
        if user.cluster_id is None:
            user.cluster_id = cluster.id
        elif user.cluster_id != cluster.id:
            logger.warning(
                "Researcher request %s approved for cluster %s but user %s stays "
                "in cluster %s (not moved automatically)",
                request.id, cluster.id, user.id, user.cluster_id,
            )

        return cluster

    def _revoke_cluster_access(self, request: ResearcherRequest, approved_cluster_id: int | None,
                               new_status: ResearcherRequestStatus) -> None:
        """Approved -> pending/rejected: the access the approval granted goes.
        Nothing records whether the approval set users.cluster_id, so clear it
        only when it still equals the approved cluster. The user is always
        dropped from that cluster's cluster_admins (approval always adds it;
        else the cluster edit form re-saves the stale admin). Re-approval
        re-adds both. The cluster itself and its devices are left alone."""
        self.revoked_cluster_id = None
        if request.status != ResearcherRequestStatus.APPROVED or approved_cluster_id is None:
            return
        user = self.session.query(User).filter(User.id == request.user_id).first()
        if user is None:
            return
        cluster = self.session.get(DeviceCluster, approved_cluster_id)
        if cluster is not None and user in cluster.cluster_admins:
            cluster.cluster_admins.remove(user)
        if user.cluster_id == approved_cluster_id:
            user.cluster_id = None
            self.revoked_cluster_id = approved_cluster_id
            logger.warning(
                "Researcher request %s moved from approved to %s: user %s removed "
                "from cluster %s",
                request.id, new_status, user.id, approved_cluster_id,
            )
        elif user.cluster_id is not None:
            logger.warning(
                "Researcher request %s moved from approved to %s for cluster %s but "
                "user %s stays in cluster %s (not the request's cluster)",
                request.id, new_status, approved_cluster_id, user.id, user.cluster_id,
            )

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

        self.revoked_cluster_id = None
        if status_enum == ResearcherRequestStatus.APPROVED:
            self._ensure_cluster_for_request(request)
        else:
            self._revoke_cluster_access(request, request.cluster_id, status_enum)

        request.status = status_enum
        self.session.commit()
        self.session.refresh(request)

        return request

    def update_reseacher_request(self, request_id: int, request_data: UpdateResearcherRequest) -> ResearcherRequest:
        request = self.get_by_id(request_id)

        if not request:
            raise HTTPException(status_code=404, detail=f"Researcher request with id {request_id} not found")

        self.revoked_cluster_id = None
        # Revocation must match the cluster that was approved, not one this
        # PATCH swaps in.
        approved_cluster_id = request.cluster_id

        status_enum = None
        if request_data.status is not None:
            try:
                normalized_status = request_data.status.strip().lower()
                if normalized_status == "declined":
                    normalized_status = "rejected"
                status_enum = ResearcherRequestStatus(normalized_status)
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid status value")

        # Moving an approved request's cluster while it stays approved would
        # leave the user in the old cluster and make a later rejection revoke
        # nothing. Allowed only together with a move away from approved (the
        # revoke below then targets the OLD cluster).
        if (request_data.cluster_id is not None
                and request.status == ResearcherRequestStatus.APPROVED
                and request_data.cluster_id != request.cluster_id
                and status_enum in (None, ResearcherRequestStatus.APPROVED)):
            raise HTTPException(
                status_code=409,
                detail="Cannot change the cluster of an approved request; set status "
                       "to rejected or pending in the same request",
            )

        # Cluster first: approval below attaches the user to request.cluster_id.
        if request_data.cluster_id is not None:
            if self.session.query(DeviceCluster).filter(DeviceCluster.id == request_data.cluster_id).first() is None:
                raise HTTPException(status_code=404, detail=f"Cluster with id {request_data.cluster_id} not found")
            request.cluster_id = request_data.cluster_id

        if status_enum is not None:
            if status_enum == ResearcherRequestStatus.APPROVED:
                self._ensure_cluster_for_request(request)
            else:
                self._revoke_cluster_access(request, approved_cluster_id, status_enum)

            request.status = status_enum

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
        
    
