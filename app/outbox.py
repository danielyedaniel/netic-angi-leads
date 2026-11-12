"""Transactional outbox for outbound email (database side).

A message row is inserted in the *same transaction* that stores the lead. The worker
(``app/worker.py``) then claims due rows and sends them. Consequences:

* a crash between "lead stored" and "email sent" cannot lose the email;
* ``dedupe_key`` is UNIQUE, so replaying a webhook can never queue a second intro;
* follow-ups are just rows with a later ``send_after`` that can be cancelled.

Delivery is at-least-once: if the mail server accepted a message but we could not record
that (the process died, or the database could not be written), the row is still
'sending'. It is put back in the queue when the worker next starts, or after
``STUCK_AFTER_SECONDS``, and the message is sent a second time. (A lost or timed-out
acknowledgement is the other case: the SMTP provider sends the message again at once on
a new connection.)
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from app.db import Database
from app.repo import EV_EMAIL_CANCELLED, EV_EMAIL_QUEUED, add_lead_event
from app.util import iso, new_id

# Wait before retry N (seconds). The first retry is quick because speed-to-lead matters;
# later ones back off so a provider outage is not hammered. The last step is the pace
# at which a first response keeps being retried through a long outage (see the worker).
RETRY_BACKOFF_SECONDS = (5, 30, 120, 600, 1800)
STUCK_AFTER_SECONDS = 120


def retry_delay(attempts: int) -> timedelta:
    index = min(max(attempts, 1), len(RETRY_BACKOFF_SECONDS)) - 1
    return timedelta(seconds=RETRY_BACKOFF_SECONDS[index])


def enqueue(
    conn: sqlite3.Connection,
    *,
    lead_id: str,
    tenant_id: str,
    template: str,
    to_address: str,
    dedupe_key: str,
    send_after: datetime,
    now: datetime,
    sequence: int = 0,
    max_attempts: int = 5,
) -> str | None:
    """Queue a message. Returns its id, or None if this dedupe_key was already queued
    (which makes every caller idempotent for free)."""
    message_id = new_id("msg")
    cursor = conn.execute(
        """INSERT INTO outbox_messages
               (id, lead_id, tenant_id, template, sequence, to_address, dedupe_key,
                status, send_after, max_attempts, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
           ON CONFLICT(dedupe_key) DO NOTHING""",
        (
            message_id,
            lead_id,
            tenant_id,
            template,
            sequence,
            to_address,
            dedupe_key,
            iso(send_after),
            max_attempts,
            iso(now),
        ),
    )
    if cursor.rowcount == 0:
        return None
    add_lead_event(
        conn,
        lead_id,
        tenant_id,
        EV_EMAIL_QUEUED,
        now,
        {"message_id": message_id, "template": template, "send_after": iso(send_after)},
    )
    return message_id


def cancel_pending(
    conn: sqlite3.Connection,
    lead_id: str,
    now: datetime,
    *,
    reason: str,
    templates: tuple[str, ...] | None = None,
) -> int:
    """Cancel messages that have not gone out yet (e.g. follow-ups once the lead books)."""
    sql = "SELECT id, tenant_id, template FROM outbox_messages WHERE lead_id = ? AND status = 'pending'"
    params: list = [lead_id]
    if templates:
        sql += f" AND template IN ({','.join('?' * len(templates))})"
        params.extend(templates)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return 0
    conn.executemany(
        "UPDATE outbox_messages SET status = 'cancelled', last_error = ? WHERE id = ?",
        [(reason, row["id"]) for row in rows],
    )
    add_lead_event(
        conn,
        lead_id,
        rows[0]["tenant_id"],
        EV_EMAIL_CANCELLED,
        now,
        {"reason": reason, "count": len(rows), "templates": sorted({r["template"] for r in rows})},
    )
    return len(rows)


def claim_next(db: Database, now: datetime) -> dict | None:
    """Atomically take the next due message. First-touch messages (sequence 0) jump
    ahead of follow-ups, so a backlog of nudges can never delay a brand-new lead."""
    with db.transaction() as conn:
        row = conn.execute(
            """SELECT * FROM outbox_messages
               WHERE status = 'pending' AND send_after <= ?
               ORDER BY CASE WHEN sequence = 0 THEN 0 ELSE 1 END, send_after, created_at
               LIMIT 1""",
            (iso(now),),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE outbox_messages SET status = 'sending', locked_at = ?, attempts = attempts + 1 WHERE id = ?",
            (iso(now), row["id"]),
        )
        message = dict(row)
        message["status"] = "sending"
        message["attempts"] += 1
        return message


def requeue_stuck(db: Database, now: datetime, *, all_sending: bool = False) -> int:
    """Messages left in 'sending' by a crash become pending again.

    Normally only those claimed more than ``STUCK_AFTER_SECONDS`` ago: a younger one
    may still be on its way out. ``all_sending`` is for start-up, when nothing can be
    in flight (one worker), so every 'sending' row is known to be orphaned."""
    sql = "UPDATE outbox_messages SET status = 'pending', locked_at = NULL WHERE status = 'sending'"
    params: tuple = ()
    if not all_sending:
        sql += " AND locked_at < ?"
        params = (iso(now - timedelta(seconds=STUCK_AFTER_SECONDS)),)
    with db.transaction() as conn:
        return conn.execute(sql, params).rowcount


def requeue_failed(conn: sqlite3.Connection, message_id: str, now: datetime) -> bool:
    """Operator action: give a message we had given up on a fresh set of attempts
    (for example after fixing the mail credentials). Returns False if the message is
    not in the failed state."""
    row = conn.execute(
        "SELECT lead_id, tenant_id, template FROM outbox_messages WHERE id = ? AND status = 'failed'",
        (message_id,),
    ).fetchone()
    if row is None:
        return False
    conn.execute(
        "UPDATE outbox_messages SET status = 'pending', attempts = 0, send_after = ?, last_error = NULL, "
        "locked_at = NULL WHERE id = ?",
        (iso(now), message_id),
    )
    add_lead_event(
        conn, row["lead_id"], row["tenant_id"], EV_EMAIL_QUEUED, now,
        {"message_id": message_id, "template": row["template"], "send_after": iso(now), "requeued_by_operator": True},
    )
    return True


def next_due_at(db: Database) -> str | None:
    with db.read() as conn:
        row = conn.execute(
            "SELECT MIN(send_after) AS due FROM outbox_messages WHERE status = 'pending'"
        ).fetchone()
        return row["due"]
