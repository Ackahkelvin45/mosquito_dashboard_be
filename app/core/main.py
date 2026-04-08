# main.py
import os
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from sqlalchemy import text
from app.core.database import engine
from utils.init_db import create_tables
from app.authentication.routes import router as authentication_router
from app.mosquito.routes import router as mosquito_router
from app.device.routes import router as device_router
from utils.protected_route import get_current_user
from app.authentication.schema import UserResponse
from fastapi.middleware.cors import CORSMiddleware
from app.core.mqtt_client import mqtt  



@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    try:
        create_tables()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            print("Database connection successful")
    except Exception as e:
        print(f"Database connection failed: {e}")
        raise e
    try:
        await mqtt.mqtt_startup()  # <-- start MQTT client
        print("MQTT client started")
    except Exception as e:
        print(f"Failed to start MQTT client: {e}")
      
    yield  # app runs here

    # Shutdown
    await mqtt.mqtt_shutdown()  # <-- stop MQTT client
    print("MQTT client stopped")
    print("Shutting down application...")


# ---------------------------
# Create FastAPI app
# ---------------------------
def create_application() -> FastAPI:
    app = FastAPI(
        title="Mosquito Dashboard API",
        version="1.0.0",
        lifespan=lifespan
    )

    mqtt.init_app(app)  # <-- bind FastMQTT to the app instance

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000","https://mosquitosurveillancedashboard.website"],  # TODO: Change to specific origins
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/")
    async def health_check():
        return {"status": "ok"}

    @app.get("/protected")
    async def protected(user: UserResponse = Depends(get_current_user)):
        return {"message": "Protected route", "user": user}

    return app


app = create_application()

app.include_router(authentication_router, tags=["authentication"], prefix="/auth")
app.include_router(device_router, tags=["devices"], prefix="/devices")
app.include_router(mosquito_router, tags=["mosquito"], prefix="/mosquito")


# ---------------------------
# Run app with Uvicorn
# ---------------------------
if __name__ == "__main__":
    host = os.getenv("APP_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", 8000))

    uvicorn.run(
        "app.core.main:app",
        host=host,
        port=port,
        reload=True
    )