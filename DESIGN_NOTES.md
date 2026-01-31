# Design Notes: Webhook to Postgres Connector

This document explains the key technical decisions I made when building this webhook processing system, specifically around idempotency, retry logic, and preventing race conditions.

## 1. Idempotency: Where and How It Works

The idempotency guarantee operates at two layers - the database and the application.

### Database Layer

The core protection comes from a unique constraint on the events table:
```sql
CREATE TABLE events (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(255) UNIQUE NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    occurred_at TIMESTAMP NOT NULL,
    payload JSON NOT NULL,
    payload_hash VARCHAR(64),
    created_at TIMESTAMP DEFAULT NOW()
);
```

The unique constraint on `event_id` means Postgres will reject any attempt to insert a duplicate, even if multiple requests arrive simultaneously.

### Application Layer

On top of the database constraint, the application does an explicit check before inserting:
```python
# Compute SHA-256 hash of the payload
payload_hash = hashlib.sha256(
    json.dumps(payload, sort_keys=True).encode()
).hexdigest()

# Check if this event_id already exists
existing_event = db.query(Event).filter(
    Event.event_id == event_id
).first()

if existing_event:
    # Same payload = deduplicate silently
    if existing_event.payload_hash == payload_hash:
        return {"status": "deduplicated"}
    
    # Different payload = flag as conflict
    if existing_event.payload_hash != payload_hash:
        return {"status": "conflict"}
```

### Why This Approach

I chose this two-layer approach because:

1. The database constraint is the ultimate source of truth. Even if there's a bug in my application code, or if multiple API instances race each other, the database will enforce uniqueness.

2. The application-level check lets me provide better error messages and detect payload conflicts. Without it, I'd only catch duplicates when the database throws an IntegrityError.

3. The payload hash allows efficient conflict detection without comparing entire JSON blobs in the database.

The tradeoff is an extra database round-trip for the duplicate check, but I think it's worth it for correctness and better error reporting.

## 2. Retry Logic with Exponential Backoff

When processing fails (the third-party API is down, network issue, etc.), the system retries with increasing delays.

### The Algorithm
```python
def calculate_backoff_delay(attempt: int) -> int:
    delay = initial_delay * (2 ** (attempt - 1))
    return min(delay, max_delay)
```

This gives you:
- Attempt 1: Immediate
- Attempt 2: Wait 1 second
- Attempt 3: Wait 2 seconds
- Attempt 4: Wait 4 seconds
- Attempt 5: Wait 8 seconds

After 5 failed attempts, the event stays in "failed" state. You could extend this to move permanently failed events to a dead letter queue, but for this implementation I kept it simple.

### How It Works

The worker polls the database every 2 seconds looking for events with `status='pending'`:
```python
while True:
    pending_events = db.query(ProcessingState).filter(
        ProcessingState.status == 'pending'
    ).limit(10).all()
    
    for event in pending_events:
        await process_event(event.event_id)
    
    await asyncio.sleep(2)
```

When processing an event:
```python
async def process_event(event_id: str):
    # Lock the row so no other worker can grab it
    processing_state = db.query(ProcessingState).filter(
        ProcessingState.event_id == event_id,
        ProcessingState.status.in_(['pending', 'failed'])
    ).with_for_update().first()
    
    if not processing_state:
        return  # Already being processed
    
    # Mark as processing
    processing_state.status = 'processing'
    processing_state.attempt_count += 1
    db.commit()
    
    # Try calling the API
    success = await call_third_party_api(event_id)
    
    if success:
        processing_state.status = 'completed'
        db.commit()
    else:
        processing_state.status = 'failed'
        db.commit()
        
        # Schedule retry if we haven't hit the limit
        if processing_state.attempt_count < max_attempts:
            backoff_delay = calculate_backoff_delay(
                processing_state.attempt_count
            )
            await asyncio.sleep(backoff_delay)
            
            processing_state.status = 'pending'
            db.commit()
```

### Why Exponential Backoff

The exponential backoff is standard practice because:

1. It gives downstream services time to recover from issues
2. It prevents hammering a service that's already struggling
3. It's what every major API (Stripe, AWS, etc.) recommends

The downside is events can take a while to process if the API keeps failing. For my use case (webhooks), eventual consistency within a few minutes is acceptable. If you needed faster guarantees, you'd want a proper message queue like RabbitMQ or AWS SQS instead of polling.

## 3. Preventing Race Conditions and Double Processing

The biggest challenge with multiple workers is making sure the same event isn't processed twice simultaneously.

### The Solution: Database Row Locking

I use Postgres's `SELECT FOR UPDATE` to acquire an exclusive lock:
```python
processing_state = db.query(ProcessingState).filter(
    ProcessingState.event_id == event_id,
    ProcessingState.status.in_(['pending', 'failed'])
).with_for_update().first()
```

When Worker A executes this query, it locks that specific row. If Worker B tries to lock the same row, it has to wait until Worker A commits or rolls back the transaction.

### Multiple Layers of Protection

I actually have three layers protecting against double processing:

**Layer 1: The row lock**
Only one worker can hold the lock at a time.

**Layer 2: Status checks**
Workers only grab events in 'pending' or 'failed' status. Once an event moves to 'processing', other workers won't touch it.

**Layer 3: Unique constraint**
The processing_state table has a unique constraint on event_id. Even if the application has a bug and tries to create duplicate entries, the database will reject them.

### Testing the Protection

The integration test proves this works:
```python
def test_no_double_processing():
    # Send webhook
    response = client.post("/webhook", json=webhook_payload)
    
    # Should have exactly 1 processing_state
    states = db.query(ProcessingState).filter(
        ProcessingState.event_id == "test-001"
    ).all()
    assert len(states) == 1
    
    # Try to create a duplicate
    duplicate = ProcessingState(
        event_id="test-001",
        status="pending",
        attempt_count=0
    )
    db.add(duplicate)
    
    # This should fail
    with pytest.raises(IntegrityError):
        db.commit()
```

The test confirms that the database constraint catches attempts to create duplicate processing states.

## 4. Audit Logging

Every meaningful action gets logged to the audit_log table:
```sql
CREATE TABLE audit_log (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT NOW(),
    event_id VARCHAR(255) NOT NULL,
    action VARCHAR(100) NOT NULL,
    details TEXT,
    success VARCHAR(10)
);
```

The actions tracked include:
- event_received - Webhook hit the API
- event_inserted - Stored in database
- event_deduped - Duplicate was ignored
- conflict_detected - Same event_id, different payload
- processing_attempt_started - Worker started processing
- processing_attempt_failed - Processing failed
- processing_succeeded - Processing completed
- retry_scheduled - Retry queued with backoff

This gives you a complete audit trail. You can see exactly what happened to any event by querying the audit log. This is crucial for debugging production issues and also helps with compliance requirements if you need to prove what happened to each webhook.

## 5. Technology Choices

**Postgres** - I needed ACID transactions, row-level locking, and JSON support. Postgres is battle-tested and gives you all of this out of the box.

**FastAPI** - Modern Python framework with good async support and automatic API documentation. The type hints and Pydantic validation catch a lot of bugs before they hit production.

**SQLAlchemy** - Provides a nice abstraction over raw SQL while still letting you use database-specific features like SELECT FOR UPDATE when needed.

**Docker Compose** - Makes the whole system reproducible. Anyone can clone the repo and have it running in minutes with docker compose up.

## 6. What I'd Change for Production

This implementation works well for moderate scale, but if I were deploying this to handle millions of webhooks, here's what I'd improve:

**Replace polling with a real queue** - Right now the worker polls the database every 2 seconds. This works but isn't as efficient as a proper message queue. I'd use Redis with Arq, or RabbitMQ, or AWS SQS.

**Add observability** - Metrics with Prometheus for tracking success rates and latency. Distributed tracing with OpenTelemetry. Alerting with PagerDuty when error rates spike.

**Dead letter queue** - Events that fail all retries should go to a dead letter queue for manual review rather than just sitting in "failed" state forever.

**Partitioning** - Partition the events table by date for better query performance. Archive old events to cold storage.

**Rate limiting** - Protect the downstream API from getting overwhelmed if there's a sudden spike in webhooks.

## 7. Testing Strategy

I focused on integration tests rather than unit tests because the whole point of this system is how the pieces work together. The tests prove:

1. **Idempotency** - Send the same webhook 10 times, only get 1 database row
2. **Retry logic** - Failed attempts are logged and retried
3. **Race conditions** - Database constraints prevent duplicate processing
4. **Conflict detection** - Different payloads with same event_id are caught

These integration tests run against the real Postgres database, which is important because you can't really test database constraints with mocks.

## Summary

The core design principles were:

- Use database constraints as the source of truth for uniqueness
- Layer application logic on top for better error messages
- Exponential backoff for resilient retry behavior
- Row-level locking to prevent race conditions
- Comprehensive audit logging for debugging and compliance

The system prioritizes correctness over raw speed. For webhook processing, I think that's the right tradeoff - it's more important that every webhook gets processed exactly once than that it happens in milliseconds.
