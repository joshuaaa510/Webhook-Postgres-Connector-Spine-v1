# Webhook → Postgres Connector Spine v1

A production-ready webhook receiver service that stores events idempotently in PostgreSQL and processes them reliably with retries, exponential backoff, and comprehensive audit logging.

## Quick Start

### Prerequisites
- Docker Desktop installed
- Git

### Run the System
```bash
# Clone the repository
git clone https://github.com/joshuaaa510/Webhook-Postgres-Connector-Spine-v1.git
cd Webhook-Postgres-Connector-Spine-v1

# Start all services
docker compose up --build

# The API will be available at http://localhost:8000
```

### Run Tests
```bash
# Run integration tests (while docker compose is running)
docker compose exec api pytest tests/ -v -s

# Or run a specific test
docker compose exec api pytest tests/test_integration.py::test_idempotency -v -s
```

##  Live Demo

**Dashboard:** https://webhook-postgres-connector-spine-v1-production.up.railway.app/dashboard

Try sending webhooks and watch them process in real-time!

## What's Included

This system demonstrates production-ready patterns for:

**Idempotent storage** - Same event 10x = 1 database row  
**Conflict detection** - Detects payload changes for same event_id  
**Reliable processing** - Retries with exponential backoff  
**No double-processing** - Database locking prevents race conditions  
**Comprehensive auditing** - Every action logged in audit_log table  
**Integration tests** - Proves idempotency, retries, and locking work  
**Visual dashboard** - Real-time monitoring of events and processing

## 🏗️ Architecture
```
┌─────────────┐
│   Client    │
└──────┬──────┘
       │ POST /webhook
       ▼
┌─────────────────────────────────────────────────────────┐
│                     API Service                          │
│  ┌───────────────────────────────────────────────────┐  │
│  │  1. Validate webhook                              │  │
│  │  2. Compute payload hash                          │  │
│  │  3. Check for existing event_id (idempotency)    │  │
│  │  4. Insert event + processing_state (atomic)     │  │
│  │  5. Log to audit_log                             │  │
│  │  6. Return 200 quickly                           │  │
│  └───────────────────────────────────────────────────┘  │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
         ┌────────────────────────┐
         │   PostgreSQL Database   │
         │  ┌──────────────────┐  │
         │  │ events           │  │  Unique constraint on event_id
         │  │ audit_log        │  │  Records every action
         │  │ processing_state │  │  Prevents double-processing
         │  └──────────────────┘  │
         └──────────┬─────────────┘
                    │
                    ▼
         ┌─────────────────────────┐
         │   Background Worker      │
         │  ┌───────────────────┐  │
         │  │ Poll for pending  │  │
         │  │ Acquire lock      │  │
         │  │ Process event     │  │
         │  │ Retry on failure  │  │
         │  │ Exponential       │  │
         │  │ backoff           │  │
         │  └───────────────────┘  │
         └──────────┬──────────────┘
                    │
                    ▼
         ┌─────────────────────────┐
         │  Mock Third-Party API    │
         │  (randomly fails 50%)    │
         └─────────────────────────┘
```

##  Project Structure
```
webhook-connector/
├── app/
│   ├── __init__.py
│   ├── main.py           # FastAPI webhook receiver
│   ├── worker.py         # Background job processor
│   ├── mock_api.py       # Simulated third-party API
│   ├── models.py         # Database models
│   ├── database.py       # Database connection
│   └── config.py         # Configuration
├── tests/
│   ├── __init__.py
│   └── test_integration.py  # Integration tests
├── docker-compose.yml    # Service orchestration
├── Dockerfile           # Container image
├── dashboard.html       # Visual monitoring dashboard
├── requirements.txt     # Python dependencies
├── DESIGN_NOTES.md      # Technical design decisions
└── README.md           # This file
```

##  How It Works

### 1. Idempotency (No Duplicate Writes)

**Database Constraint:**
```sql
CREATE TABLE events (
    event_id VARCHAR(255) UNIQUE NOT NULL,
    ...
);
```

**Code Logic:**
- Check if event_id exists before inserting
- If exists with same payload → return "deduplicated"
- If exists with different payload → return "conflict"
- Use `payload_hash` (SHA-256) for efficient conflict detection

**Race Condition Protection:**
- Unique constraint on `event_id` catches concurrent inserts
- IntegrityError handled gracefully → deduplicated response

### 2. Retry Logic with Exponential Backoff

**Strategy:**
```
Attempt 1: Immediate
Attempt 2: Wait 1s
Attempt 3: Wait 2s  
Attempt 4: Wait 4s
Attempt 5: Wait 8s (max)
```

**Implementation:**
- Worker polls for `status='pending'` events
- Updates to `status='processing'` with database lock
- On failure: status → 'failed', schedule retry
- On success: status → 'completed'
- Max 5 attempts (configurable)

### 3. Preventing Double-Processing

**Database Lock:**
```python
processing_state = db.query(ProcessingState).filter(
    ProcessingState.event_id == event_id,
    ProcessingState.status.in_(['pending', 'failed'])
).with_for_update().first()
```

**Protections:**
- `event_id` unique constraint on `processing_state` table
- `SELECT FOR UPDATE` prevents concurrent workers
- Status transitions: pending → processing → completed/failed

### 4. Audit Log

**Every action is logged:**
- `event_received` - Webhook arrived
- `event_inserted` - Stored in database
- `event_deduped` - Duplicate ignored
- `conflict_detected` - Payload mismatch
- `processing_attempt_started` - Worker started processing
- `processing_attempt_failed` - Processing failed (with reason)
- `processing_succeeded` - Processing completed
- `retry_scheduled` - Retry queued with backoff

## 🧪 Integration Tests

### Test 1: Idempotency
```bash
docker compose exec api pytest tests/test_integration.py::test_idempotency -v -s
```
**Proves:** Send same webhook 10x → only 1 row in events table

### Test 2: Retry Logic
```bash
docker compose exec api pytest tests/test_integration.py::test_retry_with_eventual_success -v -s
```
**Proves:** Failed API calls retry with exponential backoff, audit log shows all attempts

### Test 3: No Double-Processing
```bash
docker compose exec api pytest tests/test_integration.py::test_no_double_processing -v -s
```
**Proves:** Database constraints prevent duplicate processing_state entries

### Test 4: Conflict Detection (Bonus)
```bash
docker compose exec api pytest tests/test_integration.py::test_payload_conflict_detection -v -s
```
**Proves:** Same event_id with different payload is detected and logged

## 📊 API Endpoints

### Webhook Receiver
```bash
POST /webhook
Content-Type: application/json

{
  "event_id": "test-001",
  "event_type": "user.created",
  "occurred_at": "2024-01-30T12:00:00Z",
  "payload": {
    "user_id": 123,
    "email": "test@example.com"
  }
}
```

### Dashboard & Monitoring
- `GET /` - Health check
- `GET /dashboard` - Visual monitoring interface
- `GET /api/events` - List recent events
- `GET /api/audit` - View audit log
- `GET /api/processing` - Check processing states

## 📊 Observability

### View Logs
```bash
# API logs
docker compose logs -f api

# Worker logs
docker compose logs -f worker

# All logs
docker compose logs -f
```

### Check Database
```bash
# Connect to Postgres
docker compose exec db psql -U postgres -d webhook_connector

# Query events
SELECT event_id, event_type, created_at FROM events;

# Query audit log
SELECT event_id, action, success, timestamp FROM audit_log ORDER BY timestamp DESC LIMIT 20;

# Query processing state
SELECT event_id, status, attempt_count, last_attempt_at FROM processing_state;
```

## ⚙️ Configuration

All configuration via environment variables (see `app/config.py`):
```bash
DATABASE_URL=postgresql://postgres:postgres@db:5432/webhook_connector
REDIS_URL=redis://redis:6379
MOCK_API_URL=http://mock-api:8001
MOCK_API_FAILURE_RATE=0.5  # 50% failure rate for testing
MAX_RETRY_ATTEMPTS=5
INITIAL_RETRY_DELAY=1  # seconds
MAX_RETRY_DELAY=60  # seconds
LOG_LEVEL=INFO
```

## 🛑 Stopping the System
```bash
# Stop all services
docker compose down

# Stop and remove volumes (clean slate)
docker compose down -v
```

## 📈 Production Considerations

This implementation includes production-ready patterns:

1. **Connection Pooling** - SQLAlchemy with pool_pre_ping
2. **Health Checks** - Database and Redis health checks in docker-compose
3. **Graceful Degradation** - Worker continues on individual event failures
4. **Idempotent APIs** - Safe to retry webhook deliveries
5. **Audit Trail** - Complete history of all operations
6. **Configurable Retries** - Tune retry behavior via environment variables
7. **Database Transactions** - Atomic operations prevent partial state
8. **Logging** - Structured logging for debugging in production

##  Design Decisions

See [DESIGN_NOTES.md](DESIGN_NOTES.md) for detailed technical design decisions including:
- Where idempotency lives (database + application layers)
- How retries work (exponential backoff algorithm)
- How we prevent race conditions (database locking + unique constraints)
- Technology choices and tradeoffs

## 📝 License

MIT License

##  Author

Joshua - Built for Technical Assessment

**Repository:** https://github.com/joshuaaa510/Webhook-Postgres-Connector-Spine-v1
