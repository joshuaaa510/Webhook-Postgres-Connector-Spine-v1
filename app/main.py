from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
import hashlib
import json
import logging
from typing import Optional

from app.database import get_db, init_db
from app.models import Event, AuditLog, ProcessingState
from app.config import get_settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

settings = get_settings()

# Initialize FastAPI app
app = FastAPI(
    title="Webhook Postgres Connector Spine v1",
    description="Production-ready webhook receiver with idempotent storage and reliable processing"
)


class WebhookPayload(BaseModel):
    """Webhook request schema"""
    event_id: str = Field(..., description="Unique event identifier")
    event_type: str = Field(..., description="Type of event")
    occurred_at: datetime = Field(..., description="When the event occurred (ISO timestamp)")
    payload: dict = Field(..., description="Event payload data")


def create_audit_log(db: Session, event_id: str, action: str, details: str = None, success: str = None):
    """Helper to create audit log entries"""
    audit = AuditLog(
        event_id=event_id,
        action=action,
        details=details,
        success=success
    )
    db.add(audit)
    db.commit()
    logger.info(f"Audit: {action} | event_id={event_id} | success={success}")


def compute_payload_hash(payload: dict) -> str:
    """Compute hash of payload for conflict detection"""
    payload_str = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(payload_str.encode()).hexdigest()


@app.on_event("startup")
async def startup_event():
    """Initialize database on startup"""
    logger.info("Initializing database...")
    init_db()
    logger.info("Database initialized successfully")


@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "service": "Webhook Postgres Connector Spine v1",
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat()
    }


@app.post("/webhook")
async def receive_webhook(
    webhook: WebhookPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Receive webhook events and store them idempotently.
    
    - Validates incoming payload
    - Stores event with idempotent event_id (no duplicates)
    - Detects payload conflicts
    - Enqueues background processing job
    - Returns 200 quickly
    """
    event_id = webhook.event_id
    
    # Log webhook received
    logger.info(f"Webhook received: event_id={event_id}, event_type={webhook.event_type}")
    create_audit_log(db, event_id, "event_received", f"Type: {webhook.event_type}", "pending")
    
    # Compute payload hash for conflict detection
    payload_hash = compute_payload_hash(webhook.payload)
    
    try:
        # Check if event already exists
        existing_event = db.query(Event).filter(Event.event_id == event_id).first()
        
        if existing_event:
            # Event already exists - check for payload conflict
            if existing_event.payload_hash != payload_hash:
                # Payload has changed - this is a conflict
                conflict_msg = f"Event {event_id} already exists with different payload"
                logger.warning(conflict_msg)
                create_audit_log(db, event_id, "conflict_detected", conflict_msg, "failure")
                return {
                    "status": "conflict",
                    "message": conflict_msg,
                    "event_id": event_id
                }
            else:
                # Exact duplicate - idempotent behavior
                logger.info(f"Duplicate event ignored (idempotent): event_id={event_id}")
                create_audit_log(db, event_id, "event_deduped", "Duplicate ignored", "success")
                return {
                    "status": "deduplicated",
                    "message": "Event already processed",
                    "event_id": event_id
                }
        
        # Insert new event
        new_event = Event(
            event_id=event_id,
            event_type=webhook.event_type,
            occurred_at=webhook.occurred_at,
            payload=webhook.payload,
            payload_hash=payload_hash
        )
        db.add(new_event)
        
        # Create processing state entry
        processing_state = ProcessingState(
            event_id=event_id,
            status="pending",
            attempt_count=0
        )
        db.add(processing_state)
        
        db.commit()
        
        logger.info(f"Event stored successfully: event_id={event_id}")
        create_audit_log(db, event_id, "event_inserted", "Event stored in database", "success")
        
        # Enqueue background processing (we'll implement this with Arq worker)
        # For now, we'll use a simple background task
        # In production with Arq, we'd use: await arq_redis.enqueue_job('process_event', event_id)
        background_tasks.add_task(trigger_processing, event_id)
        
        return {
            "status": "accepted",
            "message": "Event received and queued for processing",
            "event_id": event_id
        }
        
    except IntegrityError as e:
        db.rollback()
        # Race condition - another request inserted the same event_id
        logger.warning(f"IntegrityError on event_id={event_id}: {str(e)}")
        create_audit_log(db, event_id, "event_deduped", "Race condition detected", "success")
        return {
            "status": "deduplicated",
            "message": "Event already processed (race condition)",
            "event_id": event_id
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Error processing webhook: {str(e)}")
        create_audit_log(db, event_id, "event_received", f"Error: {str(e)}", "failure")
        raise HTTPException(status_code=500, detail=str(e))


def trigger_processing(event_id: str):
    """
    Trigger background processing.
    In production, this would enqueue to Arq/Celery.
    For docker-compose, the worker will poll for pending events.
    """
    logger.info(f"Processing triggered for event_id={event_id}")
    # The worker.py will handle actual processing


# Dashboard endpoint - serves the dashboard.html file
@app.get("/dashboard")
async def serve_dashboard():
    """Serve the dashboard HTML file"""
    return FileResponse("dashboard.html")


# API endpoints for dashboard data
@app.get("/api/events")
async def get_events(db: Session = Depends(get_db)):
    """Get all events"""
    events = db.query(Event).order_by(Event.created_at.desc()).limit(50).all()
    return [{
        "event_id": e.event_id,
        "event_type": e.event_type,
        "created_at": str(e.created_at),
        "payload": e.payload
    } for e in events]


@app.get("/api/audit")
async def get_audit_log(db: Session = Depends(get_db)):
    """Get audit log"""
    logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(100).all()
    return [{
        "timestamp": str(l.timestamp),
        "event_id": l.event_id,
        "action": l.action,
        "details": l.details,
        "success": l.success
    } for l in logs]


@app.get("/api/processing")
async def get_processing_state(db: Session = Depends(get_db)):
    """Get processing states"""
    states = db.query(ProcessingState).order_by(ProcessingState.updated_at.desc()).limit(50).all()
    return [{
        "event_id": s.event_id,
        "status": s.status,
        "attempt_count": s.attempt_count,
        "last_attempt_at": str(s.last_attempt_at) if s.last_attempt_at else None,
        "completed_at": str(s.completed_at) if s.completed_at else None,
        "error_message": s.error_message
    } for s in states]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
