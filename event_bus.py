#!/usr/bin/env python3
"""
Event bus for SH3 monitoring system.
Provides thread-safe communication between collectors and the master coordinator.
"""
import queue
import threading
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Optional

# Thread-safe event queue (consumed by the master coordinator)
event_queue = queue.Queue()

# Subscriber queues: every published event is copied to each subscriber in
# addition to the main queue. Used by modules that need their own view of the
# full event stream (e.g. the patrol log writer) without stealing events from
# the master.
_subscribers = []
_subscriber_lock = threading.Lock()

@dataclass
class Event:
    """Base class for all events."""
    event_type: str
    timestamp: datetime
    data: Any
    
    def __str__(self):
        return f"[{self.timestamp.strftime('%H:%M:%S')}] {self.event_type}: {self.data}"

class EventTypes:
    """Constants for event types."""
    SHIP_SPOTTED = "ship_spotted"
    AIRCRAFT_SPOTTED = "aircraft_spotted"
    MESSAGE_RECEIVED = "message_received"
    WEATHER = "weather"
    SPEED_UPDATE = "speed_update"
    HEADING_UPDATE = "heading_update"
    DEPTH_UPDATE = "depth_update"
    DEPTH_UNDER_KEEL_UPDATE = "depth_under_keel_update"
    LOCATION_UPDATE = "location_update"
    CLOCK_UPDATE = "clock_update"
    CAPTAINS_LOG = "captains_log"
    CREW_UPDATE = "crew_update"
    SEARCH_STATUS = "search_status"
    HEARTBEAT = "heartbeat"
    ERROR = "error"
    STATUS = "status"

def subscribe() -> "queue.Queue[Event]":
    """
    Register a subscriber queue that receives a copy of every published event.
    Returns a queue.Queue; read events from it with get(timeout=...).
    Thread-safe - can be called from any thread.
    """
    sub_queue = queue.Queue()
    with _subscriber_lock:
        _subscribers.append(sub_queue)
    return sub_queue


def publish_event(event_type: str, data: Any):
    """
    Publish an event to the event bus.
    Thread-safe - can be called from any thread.
    The event is delivered to the main queue and to every subscriber queue.
    """
    event = Event(
        event_type=event_type,
        timestamp=datetime.now(),
        data=data
    )
    event_queue.put(event)
    with _subscriber_lock:
        for sub_queue in _subscribers:
            sub_queue.put(event)
    return event

def get_event(timeout: Optional[float] = None) -> Optional[Event]:
    """
    Get the next event from the queue.
    If timeout is None, blocks until an event is available.
    If timeout is provided, returns None if no event is available within timeout.
    """
    try:
        return event_queue.get(timeout=timeout)
    except queue.Empty:
        return None

def event_available() -> bool:
    """Check if there are events in the queue without blocking."""
    return not event_queue.empty()

def clear_queue():
    """Clear all events from the queue."""
    while not event_queue.empty():
        try:
            event_queue.get_nowait()
        except queue.Empty:
            break
