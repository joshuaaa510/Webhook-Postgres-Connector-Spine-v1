from fastapi import FastAPI
import random
import logging
from app.config import get_settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

settings = get_settings()

app = FastAPI(title="Mock Third-Party API")


@app.get("/")
async def root():
    """Health check"""
    return {"service": "Mock Third-Party API", "status": "healthy"}


@app.post("/third_party/mock")
async def mock_endpoint(payload: dict):
    """
    Mock third-party API endpoint that randomly fails.
    
    Simulates unreliable external service for testing retry logic.
    """
    event_id = payload.get("event_id", "unknown")
    
    # Randomly fail based on configured failure rate
    should_fail = random.random() < settings.mock_api_failure_rate
    
    if should_fail:
        logger.warning(f"Mock API intentionally failing for event_id={event_id}")
        return {"error": "Simulated failure"}, 500
    else:
        logger.info(f"Mock API success for event_id={event_id}")
        return {
            "status": "success",
            "event_id": event_id,
            "message": "Event processed successfully"
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
