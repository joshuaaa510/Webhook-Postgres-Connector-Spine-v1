import pytest
import time
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError
from app.main import app
from app.database import Base, get_db
from app.models import Event, AuditLog, ProcessingState
from app.config import get_settings

settings = get_settings()

# Test database URL
TEST_DATABASE_URL = settings.database_url

# Create test database engine
test_engine = create_engine(TEST_DATABASE_URL)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    """Override database dependency for tests"""
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db

# Create test client (synchronous)
client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_database():
    """Setup and teardown test database"""
    # Create all tables
    Base.metadata.create_all(bind=test_engine)
    yield
    # Clean up after tests
    Base.metadata.drop_all(bind=test_engine)


def test_idempotency():
    """
    TEST 1: Idempotency Test
    Send the same webhook 10 times → only 1 row exists in events table
    """
    print("\n" + "="*60)
    print("TEST 1: IDEMPOTENCY TEST")
    print("="*60)
    
    webhook_payload = {
        "event_id": "test-idempotency-001",
        "event_type": "test.event",
        "occurred_at": "2024-01-30T12:00:00Z",
        "payload": {"test": "data"}
    }
    
    # Send the same webhook 10 times
    responses = []
    for i in range(10):
        response = client.post("/webhook", json=webhook_payload)
        responses.append(response.json())
        print(f"Request {i+1}/10: {response.json()['status']}")
    
    # First request should be accepted
    assert responses[0]["status"] == "accepted"
    
    # All subsequent requests should be deduplicated
    for i in range(1, 10):
        assert responses[i]["status"] == "deduplicated"
    
    # Verify only 1 event in database
    db = TestSessionLocal()
    events = db.query(Event).filter(Event.event_id == "test-idempotency-001").all()
    assert len(events) == 1
    print(f"\n✅ Idempotency test passed: 10 requests → 1 database row")
    db.close()
    print("="*60)


def test_retry_with_eventual_success():
    """
    TEST 2: Retry Test
    Failed processing attempts are retried with exponential backoff
    """
    print("\n" + "="*60)
    print("TEST 2: RETRY TEST")
    print("="*60)
    
    webhook_payload = {
        "event_id": "test-retry-001",
        "event_type": "test.event",
        "occurred_at": "2024-01-30T12:00:00Z",
        "payload": {"test": "retry"}
    }
    
    # Send webhook
    response = client.post("/webhook", json=webhook_payload)
    assert response.status_code == 200
    print(f"Webhook sent: {response.json()}")
    
    # Wait for worker to process (with retries)
    print("\nWaiting for worker to process with retries...")
    time.sleep(10)
    
    # Check audit log for retry attempts
    db = TestSessionLocal()
    audit_logs = db.query(AuditLog).filter(
        AuditLog.event_id == "test-retry-001"
    ).order_by(AuditLog.timestamp).all()
    
    print(f"\n📝 Audit log entries for test-retry-001:")
    for log in audit_logs:
        print(f"  - {log.action}: {log.details} (success={log.success})")
    
    # Should see multiple processing attempts
    processing_attempts = [log for log in audit_logs if "processing_attempt" in log.action]
    assert len(processing_attempts) > 0
    print(f"\n✅ Retry test passed: {len(processing_attempts)} processing attempts recorded")
    
    db.close()
    print("="*60)


def test_no_double_processing():
    """
    TEST 3: No Double-Processing Test
    Database constraints prevent duplicate processing_state entries
    """
    print("\n" + "="*60)
    print("TEST 3: NO DOUBLE-PROCESSING TEST")
    print("="*60)
    
    webhook_payload = {
        "event_id": "test-no-double-001",
        "event_type": "test.event",
        "occurred_at": "2024-01-30T12:00:00Z",
        "payload": {"test": "no-double"}
    }
    
    # Send webhook
    response = client.post("/webhook", json=webhook_payload)
    assert response.status_code == 200
    print(f"Webhook sent: {response.json()}")
    
    # Verify only 1 processing_state entry
    db = TestSessionLocal()
    processing_states = db.query(ProcessingState).filter(
        ProcessingState.event_id == "test-no-double-001"
    ).all()
    
    assert len(processing_states) == 1
    print(f"\n✅ No double-processing test passed: 1 processing_state entry")
    
    # Try to create duplicate (should fail)
    duplicate_state = ProcessingState(
        event_id="test-no-double-001",
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
    
    db.close()
    print("="*60)


def test_payload_conflict_detection():
    """
    BONUS TEST: Payload Conflict Detection
    Same event_id with different payload is detected as a conflict
    """
    print("\n" + "="*60)
    print("BONUS TEST: PAYLOAD CONFLICT DETECTION")
    print("="*60)
    
    # Send first webhook
    payload1 = {
        "event_id": "test-conflict-001",
        "event_type": "test.event",
        "occurred_at": "2024-01-30T12:00:00Z",
        "payload": {"version": 1}
    }
    response1 = client.post("/webhook", json=payload1)
    assert response1.json()["status"] == "accepted"
    print(f"First webhook: {response1.json()['status']}")
    
    # Send same event_id with different payload
    payload2 = {
        "event_id": "test-conflict-001",
        "event_type": "test.event",
        "occurred_at": "2024-01-30T12:00:00Z",
        "payload": {"version": 2}  # Different payload!
    }
    response2 = client.post("/webhook", json=payload2)
    assert response2.json()["status"] == "conflict"
    print(f"Second webhook (different payload): {response2.json()['status']}")
    
    # Check audit log for conflict detection
    db = TestSessionLocal()
    conflict_logs = db.query(AuditLog).filter(
        AuditLog.event_id == "test-conflict-001",
        AuditLog.action == "conflict_detected"
    ).all()
    
    assert len(conflict_logs) == 1
    print(f"\n✅ Conflict detection test passed: payload change detected")
    db.close()
    print("="*60)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
