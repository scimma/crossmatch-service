"""Django initialization with PostgreSQL advisory lock.

Serializes concurrent container startups so only one runs migrations
at a time, preventing UniqueViolation errors on fresh databases.
"""

import time

import psycopg
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

# Arbitrary fixed ID for the advisory lock. All containers must use the
# same value so they coordinate on the same lock.
LOCK_ID = 74656372


class Command(BaseCommand):
    help = "Run Django initialization (migrate, periodic tasks, superuser) under a PostgreSQL advisory lock"

    def handle(self, *args, **options):
        db = settings.DATABASES['default']
        conn = self._connect_with_retry(db)
        try:
            self._acquire_lock(conn)
            self._run_init()
        finally:
            conn.close()

    def _connect_with_retry(self, db, max_attempts=5, delay=2):
        """Open a raw psycopg connection, retrying if the database isn't ready."""
        conninfo = psycopg.conninfo.make_conninfo(
            host=db['HOST'],
            port=db['PORT'],
            dbname=db['NAME'],
            user=db['USER'],
            password=db['PASSWORD'],
        )
        for attempt in range(1, max_attempts + 1):
            try:
                return psycopg.connect(conninfo, autocommit=True)
            except psycopg.OperationalError:
                if attempt == max_attempts:
                    raise
                self.stdout.write(
                    f"Database not ready, retrying in {delay}s "
                    f"(attempt {attempt}/{max_attempts})..."
                )
                time.sleep(delay)

    def _acquire_lock(self, conn, poll_interval=2):
        """Acquire a session-level advisory lock without pinning a snapshot while waiting.

        Polls the non-blocking pg_try_advisory_lock instead of the blocking
        pg_advisory_lock. A blocking pg_advisory_lock() runs as a single
        statement for the whole time it waits, which holds a transaction
        snapshot (backend_xmin) open on the waiting backend. CREATE INDEX
        CONCURRENTLY (migrations 0007/0009) waits for every such snapshot to
        clear before it can finish, so a replica parked on a blocking lock while
        the lock holder runs a concurrent-index migration deadlocks that
        migration -- and PostgreSQL does not surface it, because a CIC
        virtualxid wait is outside the deadlock detector. Polling lets a waiting
        container sit idle between attempts, holding no snapshot, so the
        migration proceeds. Still blocks until the lock is acquired.
        """
        self.stdout.write("Acquiring database initialization lock...")
        while True:
            granted = conn.execute(
                "SELECT pg_try_advisory_lock(%s)", [LOCK_ID]
            ).fetchone()[0]
            if granted:
                break
            self.stdout.write(
                f"Initialization lock held by another container; "
                f"retrying in {poll_interval}s..."
            )
            time.sleep(poll_interval)
        self.stdout.write("Lock acquired, proceeding with initialization.")

    def _run_init(self):
        """Run migrate, periodic tasks, and superuser creation."""
        self.stdout.write("Applying database migrations...")
        call_command('migrate')

        self.stdout.write("Initializing periodic tasks...")
        call_command('initialize_periodic_tasks')

        self.stdout.write("Creating Django superuser...")
        try:
            call_command('createsuperuser', '--no-input')
            self.stdout.write("Superuser created successfully.")
        except CommandError as e:
            msg = str(e)
            if "already taken" in msg:
                self.stdout.write("Superuser already exists.")
            elif "You must use --username" in msg:
                self.stdout.write("Superuser env vars not configured, skipping.")
            else:
                raise
