import asyncio
import time
import logging
import httpx
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.models import ProcessingState, AuditLog
from app.database import SessionLocal

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

settings = get_settings()


def create_audit_log(db, event_id: str, action: str, details: str = None, success: str = None):
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


async def call_third_party_api(event_id: str) -> bool:
    """
    Simulate calling a third-party API.
    Returns True on success, False on failure.
    """
    mock_url = f"{settings.mock_api_url}/third_party/mock"
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                mock_url,
                json={"event_id": event_id}
            )
            
            if response.status_code == 200:
                logger.info(f"Third-party API success for event_id={event_id}")
                return True
            else:
                logger.warning(f"Third-party API failed with {response.status_code} for event_id={event_id}")
                return False
                
    except Exception as e:
        logger.error(f"Third-party API error for event_id={event_id}: {str(e)}")
        return False


def calculate_backoff_delay(attempt: int) -> int:
    """
    Calculate exponential backoff delay.
    attempt 1: 1s
    attempt 2: 2s
    attempt 3: 4s
    attempt 4: 8s
    attempt 5: 16s (capped at max_retry_delay)
    """
    delay = settings.initial_retry_delay * (2 ** (attempt - 1))
    return min(delay, settings.max_retry_delay)


async def process_event(event_id: str):
    """
    Process a single event with retries and exponential backoff.
    
    - Attempts to acquire processing lock (prevents double-processing)
    - Calls third-party API
    - Retries on failure with exponential backoff
    - Updates processing state
    - Logs all attempts in audit_log
    """
    db = SessionLocal()
    
    try:
        # Acquire lock - update status to 'processing'
        processing_state = db.query(ProcessingState).filter(
            ProcessingState.event_id == event_id,
            ProcessingState.status.in_(['pending', 'failed'])
        ).with_for_update().first()
        
        if not processing_state:
            logger.warning(f"Event {event_id} already processing or completed")
            return
        
        # Check if we've exceeded max attempts
        if processing_state.attempt_count >= settings.max_retry_attempts:
            logger.error(f"Event {event_id} exceeded max retry attempts")
            processing_state.status = 'failed'
            processing_state.error_message = "Max retry attempts exceeded"
            db.commit()
            create_audit_log(db, event_id, "processing_abandoned", 
                           f"Max attempts ({settings.max_retry_attempts}) exceeded", "failure")
            return
        
        # Update to processing
        processing_state.status = 'processing'
        processing_state.attempt_count += 1
        processing_state.last_attempt_at = datetime.utcnow()
        db.commit()
        
        attempt_num = processing_state.attempt_count
        logger.info(f"Processing event {event_id}, attempt {attempt_num}/{settings.max_retry_attempts}")
        create_audit_log(db, event_id, "processing_attempt_started", 
                        f"Attempt {attempt_num}/{settings.max_retry_attempts}", "pending")
        
        # Call third-party API
        success = await call_third_party_api(event_id)
        
        if success:
            # Success! Mark as completed
            processing_state.status = 'completed'
            processing_state.completed_at = datetime.utcnow()
            processing_state.error_message = None
            db.commit()
            
            logger.info(f"Event {event_id} processed successfully on attempt {attempt_num}")
            create_audit_log(db, event_id, "processing_succeeded", 
                           f"Completed on attempt {attempt_num}", "success")
        else:
            # Failure - prepare for retry
            processing_state.status = 'failed'
            processing_state.error_message = "Third-party API call failed"
            db.commit()
            
            logger.warning(f"Event {event_id} failed on attempt {attempt_num}")
            create_audit_log(db, event_id, "processing_attempt_failed", 
                           f"Failed on attempt {attempt_num}", "failure")
            
            # Schedule retry if we haven't exceeded max attempts
            if attempt_num < settings.max_retry_attempts:
                backoff_delay = calculate_backoff_delay(attempt_num)
                logger.info(f"Scheduling retry for event {event_id} in {backoff_delay}s")
                create_audit_log(db, event_id, "retry_scheduled", 
                               f"Retry in {backoff_delay}s (attempt {attempt_num + 1}/{settings.max_retry_attempts})", 
                               "pending")
                
                # Sleep and retry
                await asyncio.sleep(backoff_delay)
                
                # Reset status to pending for retry
                db.refresh(processing_state)
                processing_state.status = 'pending'
                db.commit()
                
                # Recursive retry
                await process_event(event_id)
            else:
                logger.error(f"Event {event_id} failed permanently after {attempt_num} attempts")
                create_audit_log(db, event_id, "processing_failed_permanently", 
                               f"Failed after {attempt_num} attempts", "failure")
    
    except Exception as e:
        logger.error(f"Error processing event {event_id}: {str(e)}")
        try:
            create_audit_log(db, event_id, "processing_error", str(e), "failure")
        except:
            pass
    finally:
        db.close()


async def worker_loop():
    """
    Main worker loop that polls for pending events and processes them.
    """
    logger.info("Worker started - polling for pending events...")
    
    while True:
        db = SessionLocal()
        try:
            # Find pending events
            pending_events = db.query(ProcessingState).filter(
                ProcessingState.status == 'pending'
            ).limit(10).all()
            
            if pending_events:
                logger.info(f"Found {len(pending_events)} pending events")
                
                # Process events concurrently
                tasks = [process_event(event.event_id) for event in pending_events]
                await asyncio.gather(*tasks, return_exceptions=True)
            
            db.close()
            
            # Poll every 2 seconds
            await asyncio.sleep(2)
            
        except Exception as e:
            logger.error(f"Worker loop error: {str(e)}")
            db.close()
            await asyncio.sleep(5)


if __name__ == "__main__":
    logger.info("Starting worker...")
    asyncio.run(worker_loop())
  
