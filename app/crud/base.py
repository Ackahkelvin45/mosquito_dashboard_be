from typing import Type, TypeVar, Generic, List, Optional
from sqlalchemy.orm import Session
from app.core.database import Base

ModelType = TypeVar("ModelType", bound=Base)

class BaseRepository(Generic[ModelType]):
    model: Type[ModelType] = None  # child must define this

    def __init__(self, session: Session):
        self.session = session

    def get_by_id(self, id: int) -> Optional[ModelType]:
        return (
            self.session.query(self.model)
            .filter(self.model.id == id)
            .first()
        )

    def get_all(self) -> List[ModelType]:
        return self.session.query(self.model).all()


    def delete(self, id: int) -> Optional[ModelType]:
        instance = self.get_by_id(id)
        if instance:
            self.session.delete(instance)
            self.session.commit()
        return instance

    def exists(self, **filters) -> bool:
        return (
            self.session.query(self.model)
            .filter_by(**filters)
            .first()
            is not None
        )