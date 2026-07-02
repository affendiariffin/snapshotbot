import os

import psycopg
from psycopg.rows import dict_row

# Retention window for sessions and everything under them (Fendi: 30 days, no archive).
RETENTION_DAYS = 30


def get_conn():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init_db():
    # Task 2 creates the sb_* tables + expiry sweep here.
    pass
