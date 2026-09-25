from queue_load_test.config import Settings
from queue_load_test.main import run


async def test_phase_one_async_entry_point_accepts_valid_settings() -> None:
    await run(Settings(STAGING_URL="https://staging.example.test"))
