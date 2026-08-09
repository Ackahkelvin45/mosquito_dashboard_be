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
    CORS_ORIGINS: str = "http://localhost:3000,https://mosquitosurveillancedashboard.website"
    # Log every SQL statement — set SQL_ECHO=true in .env when debugging locally.
    SQL_ECHO: bool = False

    class Config:
        env_file = ".env"

settings = Settings()