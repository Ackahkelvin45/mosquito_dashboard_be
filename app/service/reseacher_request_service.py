from app.authentication.repository.researcher_request_repository import ResearcherRequestRepository
from app.authentication.schema import ResearcherRequestCreate, ResearcherRequestResponse,UpdateResearcherRequest
from fastapi import HTTPException
from sqlalchemy.orm import Session
from app.authentication.enums import ResearcherRequestStatus




class ResearcherRequestService:
    def __init__(self, session: Session):
        self.researcher_request_repository = ResearcherRequestRepository(session)

    def get_reseachers_requests(self) -> list[ResearcherRequestResponse]:
        requests = self.researcher_request_repository.get_researcher_requests()
        return [ResearcherRequestResponse.model_validate(r) for r in requests]

    # Backwards-compatible alias (older route typo)
    def get_reseachers_request(self) -> list[ResearcherRequestResponse]:
        return self.get_reseachers_requests()

    def create_researcher_request(self, request_data: ResearcherRequestCreate) -> ResearcherRequestResponse:
        researcher_request = self.researcher_request_repository.create_researcher_request(request_data)
        return ResearcherRequestResponse.model_validate(researcher_request)

    def update_researcher_request_status(self, request_id: int, status: ResearcherRequestStatus) -> ResearcherRequestResponse:
        researcher_request = self.researcher_request_repository.update_researcher_request_status(request_id, status)
        return ResearcherRequestResponse.model_validate(researcher_request)
    
    def update_reseacher_request(self, request_id: int, request_data: UpdateResearcherRequest) -> ResearcherRequestResponse:
        researcher_request = self.researcher_request_repository.update_reseacher_request(request_id, request_data)
        return ResearcherRequestResponse.model_validate(researcher_request)
