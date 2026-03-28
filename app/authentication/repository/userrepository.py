from app.crud.base import BaseRepository
from app.authentication.models import User
from app.authentication.schema import UserCreate

class UserRepository(BaseRepository[User]):
    model = User
    def create_user(self, user_data: UserCreate):
        data = user_data.model_dump(exclude_none=True,)
        data["hashed_password"] = data.pop("password")
        new_user = User(**data)
        self.session.add(new_user)
        self.session.commit()   
        self.session.refresh(new_user)
        return new_user

    def user_exists_by_email(self, email: str) -> bool:
        return self.session.query(User).filter(User.email == email).first() is not None

    def get_user_by_email(self, email: str) -> User | None:
        return self.session.query(User).filter(User.email == email).first()

    def get_user_by_id(self, user_id: int) -> User | None:
        return self.session.query(User).filter(User.id == user_id).first()
    

    def get_all_users(self) -> list[User]:
        return self.session.query(User).all()



  
    

