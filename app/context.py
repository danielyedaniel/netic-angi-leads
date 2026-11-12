"""Wires the pieces together. One ``AppContext`` holds every long-lived object, so
routes and tests get their collaborators from a single, explicit place."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.alerts import AlertNotifier
from app.config import Settings
from app.db import Database
from app.ingest import IngestService
from app.mailer.providers import EmailProvider, build_provider
from app.mailer.render import EmailRenderer
from app.util import utcnow
from app.worker import BackgroundWorker


@dataclass
class AppContext:
    settings: Settings
    db: Database
    email_provider: EmailProvider
    renderer: EmailRenderer
    notifier: AlertNotifier
    worker: BackgroundWorker
    ingest: IngestService
    clock: Callable[[], datetime]

    def now(self) -> datetime:
        return self.clock()


def build_context(
    settings: Settings | None = None,
    *,
    email_provider: EmailProvider | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> AppContext:
    settings = settings or Settings()
    db = Database(settings.database_path)
    db.migrate()
    provider = email_provider or build_provider(settings)
    renderer = EmailRenderer(settings.base_url)
    notifier = AlertNotifier(settings, provider)
    worker = BackgroundWorker(
        db=db, settings=settings, email_provider=provider, renderer=renderer, notifier=notifier, clock=clock
    )
    ingest = IngestService(db=db, settings=settings, clock=clock, wake_worker=worker.wake)
    worker.ingest = ingest
    return AppContext(
        settings=settings,
        db=db,
        email_provider=provider,
        renderer=renderer,
        notifier=notifier,
        worker=worker,
        ingest=ingest,
        clock=clock,
    )
