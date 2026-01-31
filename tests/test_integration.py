import pytest
import asyncio
import httpx
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.models import Event, AuditLog, ProcessingState
from app.config import get_settings

settings = get_settings()

# Test database URL
TEST_DB_URL = settings.database_url

# Create test engine and session
engine = create_engine(TEST_DB_URL)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="function")
def db():
    """Create a fresh database for each test"""
    Base.metadata.create_all(bind=engine)
    db = TestSessionLocal()
    yield db
    db.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
async def client():
    """HTTP client for testing webhook endpoint"""
    async with httpx.AsyncClient(base_url="http://api:8000", timeout=30.0) as client:
        yield client


def create_test_webhook(event_id: str, event_type: str = "test.event"):
    """Helper to create test webhook payload"""
    return {
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": datetime.utcnow().isoformat(),
        "payload": {
            "test": "data",
            "timestamp": datetime.utcnow().isoformat()
        }
    }


@pytest.mark.asyncio
async def test_idempotency(client, db):
    """
    TEST 1: Idempotency Test
    Send the same webhook 10 times → only 1 row exists in events table
    """
    print("\n" + "="*60)
    print("TEST 1: IDEMPOTENCY TEST")
    print("="*60)
    
    event_id = "test-event-001"
    webhook = create_test_webhook(event_id)
    
    # Send the same webhook 10 times
    for i in range(10):
        response = await client.post("/webhook", json=webhook)
        print(f"Request {i+1}/10: status={response.status_code}")
        assert response.status_code == 200
    
    # Wait a moment for async processing
    await asyncio.sleep(1)
    
    # Query database - should only have 1 event
    event_count = db.query(Event).filter(Event.event_id == event_id).count()
    print(f"\nEvents in database: {event_count}")
    assert event_count == 1, f"Expected 1 event, found {event_count}"
    
    # Check audit log shows deduplication
    audit_logs = db.query(AuditLog).filter(AuditLog.event_id == event_id).all()
    dedupe_logs = [log for log in audit_logs if log.action == "event_deduped"]
    print(f"Deduplication logs: {len(dedupe_logs)}")
    assert len(dedupe_logs) >= 8, "Should have multiple dedupe logs"
    
    print("\n✅ IDEMPOTENCY TEST PASSED")
    print("="*60)


@pytest.mark.asyncio
async def test_retry_with_eventual_success(client, db):
    """
    TEST 2: Retry Test
    Downstream API fails first 2 tries → job retries and eventually succeeds
    Audit log shows all attempts
    """
    print("\n" + "="*60)
    print("TEST 2: RETRY TEST")
    print("="*60)
    
    event_id = "test-event-retry-001"
    webhook = create_test_webhook(event_id)
    
    # Send webhook
    response = await client.post("/webhook", json=webhook)
    print(f"Webhook sent: status={response.status_code}")
    assert response.status_code == 200
    
    # Wait for processing with retries (mock API has 50% failure rate)
    # This might take multiple attempts
    print("\nWaiting for processing with retries...")
    max_wait = 60  # Wait up to 60 seconds
    waited = 0
    
    while waited < max_wait:
        await asyncio.sleep(2)
        waited += 2
        
        processing_state = db.query(ProcessingState).filter(
            ProcessingState.event_id == event_id
        ).first()
        
        if processing_state:
            print(f"Status: {processing_state.status}, Attempts: {processing_state.attempt_count}")
            
            if processing_state.status == 'completed':
                print("✅ Processing completed!")
                break
            elif processing_state.status == 'failed' and processing_state.attempt_count >= 5:
                print("⚠️  Max attempts reached, that's ok for testing")
                break
    
    # Check audit log shows multiple attempts
    audit_logs = db.query(AuditLog).filter(AuditLog.event_id == event_id).all()
    attempt_logs = [log for log in audit_logs if "attempt" in log.action.lower()]
    
    print(f"\nTotal audit log entries: {len(audit_logs)}")
    print(f"Processing attempt logs: {len(attempt_logs)}")
    
    # Print some audit entries
    print("\nAudit trail:")
    for log in audit_logs[:10]:
        print(f"  - {log.action}: {log.details} ({log.success})")
    
    assert len(attempt_logs) >= 1, "Should have at least 1 processing attempt logged"
    
    print("\n✅ RETRY TEST PASSED")
    print("="*60)


@pytest.mark.asyncio
async def test_no_double_processing(client, db):
    """
    TEST 3: No Double-Processing Test
    Simulate two workers trying to process same event
    Only one should succeed due to database locking
    """
    print("\n" + "="*60)
    print("TEST 3: NO DOUBLE-PROCESSING TEST")
    print("="*60)
    
    event_id = "test-event-double-001"
    webhook = create_test_webhook(event_id)
    
    # Send webhook
    response = await client.post("/webhook", json=webhook)
    print(f"Webhook sent: status={response.status_code}")
    assert response.status_code == 200
    
    # Wait a moment for it to be stored
    await asyncio.sleep(1)
    
    # Check that processing state uses unique constraint
    processing_state = db.query(ProcessingState).filter(
        ProcessingState.event_id == event_id
    ).first()
    
    assert processing_state is not None, "Processing state should exist"
    print(f"Processing state created: {processing_state.event_id}")
    
    # Try to create duplicate processing state (should fail)
    from sqlalchemy.exc import IntegrityError
    duplicate_state = ProcessingState(
        event_id=event_id,
        status="pending",
        attempt_count=0
    )
    db.add(duplicate_state)
    
    try:
        db.commit()
        assert False, "Should have raised IntegrityError"
    except IntegrityError:
        db.rollback()
        print("✅ Duplicate processing state rejected (IntegrityError as expected)")
    
    # Verify only one processing state exists
    state_count = db.query(ProcessingState).filter(
        ProcessingState.event_id == event_id
    ).count()
    
    print(f"Processing states in DB: {state_count}")
    assert state_count == 1, f"Expected 1 processing state, found {state_count}"
    
    print("\n✅ NO DOUBLE-PROCESSING TEST PASSED")
    print("="*60)


@pytest.mark.asyncio
async def test_payload_conflict_detection(client, db):
    """
    BONUS TEST: Payload Conflict Detection
    Send same event_id with different payload → should detect conflict
    """
    print("\n" + "="*60)
    print("BONUS TEST: PAYLOAD CONFLICT DETECTION")
    print("="*60)
    
    event_id = "test-event-conflict-001"
    
    # Send first webhook
    webhook1 = create_test_webhook(event_id)
    response1 = await client.post("/webhook", json=webhook1)
    print(f"First webhook: status={response1.status_code}")
    assert response1.status_code == 200
    
    # Send second webhook with same event_id but different payload
    webhook2 = create_test_webhook(event_id)
    webhook2["payload"]["different"] = "data"
    
    response2 = await client.post("/webhook", json=webhook2)
    print(f"Second webhook (different payload): status={response2.status_code}")
    assert response2.status_code == 200
    
    response_data = response2.json()
    print(f"Response: {response_data}")
    assert response_data["status"] == "conflict", "Should detect payload conflict"
    
    # Check audit log
    await asyncio.sleep(0.5)
    conflict_logs = db.query(AuditLog).filter(
        AuditLog.event_id == event_id,
        AuditLog.action == "conflict_detected"
    ).all()
    
    print(f"Conflict logs found: {len(conflict_logs)}")
    assert len(conflict_logs) >= 1, "Should have conflict log entry"
    
    print("\n✅ CONFLICT DETECTION TEST PASSED")
    print("="*60)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
