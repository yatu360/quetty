"""Browser-library exception families hidden behind the runtime backend boundary."""

from patchright.async_api import Error as PatchrightError
from patchright.async_api import TimeoutError as PatchrightTimeoutError
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

BROWSER_ERROR_TYPES = (PlaywrightError, PatchrightError)
BROWSER_TIMEOUT_ERROR_TYPES = (PlaywrightTimeoutError, PatchrightTimeoutError)
