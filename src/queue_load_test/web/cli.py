"""CLI for the local operator dashboard."""

import uvicorn

from queue_load_test.config import get_settings
from queue_load_test.repository import SQLiteSessionRepository
from queue_load_test.utils.instance_lock import InstanceLock
from queue_load_test.web.app import create_app


def main() -> None:
    settings = get_settings()
    repository = SQLiteSessionRepository(settings.database_url)
    app = create_app(
        settings=settings,
        repository=repository,
        instance_lock=InstanceLock.for_database(settings.database_url),
    )
    # uvicorn owns SIGINT/SIGTERM: it stops accepting connections, finishes
    # in-flight requests, then runs the lifespan shutdown (see ApplicationRunRuntime).
    uvicorn.run(app, host=settings.ui_host, port=settings.ui_port)


if __name__ == "__main__":
    main()
