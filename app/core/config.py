from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str
    JWT_EXPIRATION_TIME: int
    JWT_REFRESH_TOKEN_EXPIRE_SECONDS: int
    RESEND_API_KEY: str
    EMAIL_FROM: str
    MQTT_BROKER: str
    MQTT_PORT: int
    TOPIC_SENSOR_DATA: str
    TOPIC_MOSQUITO_COUNT: str
    MQTT_CLIENT_ID: str

    class Config:
        env_file = ".env"

settings = Settings()