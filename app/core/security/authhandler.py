import jwt
import os
from datetime import datetime, timedelta
from fastapi import HTTPException
from dotenv import load_dotenv

load_dotenv()

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_SECONDS = int(
    os.getenv("JWT_ACCESS_TOKEN_EXPIRE_SECONDS", 1800)
)
REFRESH_TOKEN_EXPIRE_SECONDS = int(
    os.getenv("JWT_REFRESH_TOKEN_EXPIRE_SECONDS", 604800)
)

if not JWT_SECRET_KEY:
    raise ValueError("JWT_SECRET_KEY not set in environment")

class AuthHandler:

    @staticmethod
    def create_access_token(user_id: int) -> str:
        payload = {
            "sub": str(user_id),
            "type": "access",
            "exp": datetime.utcnow() + timedelta(seconds=ACCESS_TOKEN_EXPIRE_SECONDS),
            "iat": datetime.utcnow(),
        }
        return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

    @staticmethod
    def create_refresh_token(user_id: int) -> str:
        payload = {
            "sub": str(user_id),
            "type": "refresh",
            "exp": datetime.utcnow() + timedelta(seconds=REFRESH_TOKEN_EXPIRE_SECONDS),
            "iat": datetime.utcnow(),
        }
        return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

  


    @staticmethod
    def verify_token(token: str, expected_type: str = "access") -> int:
            try:
                payload = jwt.decode(
                    token,
                    JWT_SECRET_KEY,
                    algorithms=[JWT_ALGORITHM],
                )

                # Validate token type
                if payload.get("type") != expected_type:
                    raise HTTPException(
                        status_code=401,
                        detail="Invalid token type"
                    )

                user_id = payload.get("sub")
                if not user_id:
                    raise HTTPException(
                        status_code=401,
                        detail="Invalid token payload"
                    )

                return int(user_id)

            except jwt.ExpiredSignatureError:
                raise HTTPException(status_code=401, detail="Token has expired")

            except jwt.InvalidTokenError:
                raise HTTPException(status_code=401, detail="Invalid token")
    


    @staticmethod
    def decode_token(token: str) -> dict:
        try:
            return jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token has expired")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid token")

