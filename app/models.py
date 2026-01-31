from sqlalchemy import Column, String, DateTime, JSON, Integer, Text, Index, UniqueConstraint
from sqlalchemy.sql import func
from app.database import Base
from datetime import datetime


class Event(Base):
    """Events table - stores webhook events with idempotent event_id"""
    __tablename__ = "events"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(255), unique=True, nullable=False, index=True)
    event_type = Column(String(100), nullable=False)
    occurred_at = Column(DateTime, nullable=False)
    payload = Column(JSON, nullable=False)
    payload_hash = Column(String(64), nullable=True)  # For conflict detection
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    
    __table_args__ = (
        Index('idx_event_id', 'event_id'),
    )


class AuditLog(Base):
    """Audit log table - records every important action"""
    __tablename__ = "audit_log"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, server_default=func.now(), nullable=False, index=True)
    event_id = Column(String(255), nullable=False, index=True)
    action = Column(String(100), nullable=False)
    details = Column(Text, nullable=True)
    success = Column(String(10), nullable=True)  # 'success', 'failure', 'pending'
    
    __table_args__ = (
        Index('idx_event_id_action', 'event_id', 'action'),
    )


class ProcessingState(Base):
    """Processing state table - prevents double processing with locking"""
    __tablename__ = "processing_state"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(255), unique=True, nullable=False, index=True)
    status = Column(String(50), nullable=False)  # 'pending', 'processing', 'completed', 'failed'
    attempt_count = Column(Integer, default=0, nullable=False)
    last_attempt_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    
    __table_args__ = (
        Index('idx_event_id_status', 'event_id', 'status'),
    )
