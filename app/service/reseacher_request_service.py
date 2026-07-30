from app.authentication.repository.researcher_request_repository import ResearcherRequestRepository
from app.authentication.schema import ResearcherRequestCreate, ResearcherRequestResponse,UpdateResearcherRequest
from app.core.pagination import Page, paginate
from app.notification.events import NotificationEvent, emit
from fastapi import HTTPException
from sqlalchemy.orm import Session
from app.authentication.enums import ResearcherRequestStatus




class ResearcherRequestService:
    def __init__(self, session: Session):
        self.session = session
        self.researcher_request_repository = ResearcherRequestRepository(session)

    def get_reseachers_requests(self, page: int = 1, page_size: int = 20) -> Page[ResearcherRequestResponse]:
        requests = self.researcher_request_repository.get_researcher_requests()
        sliced, total, total_pages = paginate(requests, page, page_size)
        return Page[ResearcherRequestResponse](
            items=[ResearcherRequestResponse.model_validate(r) for r in sliced],
            total=total, page=page, page_size=page_size, total_pages=total_pages,
        )

    # Backwards-compatible alias (older route typo)
    def get_reseachers_request(self, page: int = 1, page_size: int = 20) -> Page[ResearcherRequestResponse]:
        return self.get_reseachers_requests(page=page, page_size=page_size)

    def create_researcher_request(self, request_data: ResearcherRequestCreate) -> ResearcherRequestResponse:
        researcher_request = self.researcher_request_repository.create_researcher_request(request_data)
        emit(self.session, NotificationEvent.RESEARCHER_REQUEST_SUBMITTED,
             user=researcher_request.user, cluster=researcher_request.cluster)
        return ResearcherRequestResponse.model_validate(researcher_request)

    def update_researcher_request_status(self, request_id: int, status: ResearcherRequestStatus) -> ResearcherRequestResponse:
        researcher_request = self.researcher_request_repository.update_researcher_request_status(request_id, status)
        return ResearcherRequestResponse.model_validate(researcher_request)
    
    def update_reseacher_request(self, request_id: int, request_data: UpdateResearcherRequest) -> ResearcherRequestResponse:
        researcher_request = self.researcher_request_repository.update_reseacher_request(request_id, request_data)
        return ResearcherRequestResponse.model_validate(researcher_request)
