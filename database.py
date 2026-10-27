
import os
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row


def connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL ist nicht konfiguriert.")

    return psycopg.connect(url, row_factory=dict_row)


def init_database():
    with connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS premium_subscriptions (
                discord_user_id BIGINT PRIMARY KEY,
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT UNIQUE,
                status TEXT NOT NULL DEFAULT 'inactive',
                current_period_end TIMESTAMPTZ,
                cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_stripe_events (
                event_id TEXT PRIMARY KEY,
                processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS premium_status_idx
            ON premium_subscriptions(status, current_period_end)
        """)
