"""The one HTTP GET every source uses: a User-Agent, a timeout, and one retry.

Cloudflare in front of the quakecoresoft APIs refuses urllib's default
User-Agent, and both APIs occasionally answer 5xx while a container restarts, so
a request is retried once after a short pause, and only for those failures.
"""

import sys
import time
import urllib.error
import urllib.request

# Long enough for a restarting container to come back; short enough not to stall a scan.
RETRY_DELAY_S = 5.0
TIMEOUT_S = 60.0


def get(
    url: str, user_agent: str, headers: dict[str, str] | None = None, timeout: float = TIMEOUT_S
) -> tuple[int, bytes]:
    """GET a URL, retrying once on a 5xx answer, a timeout or a connection error.

    Parameters
    ----------
    url : str
        The URL.
    user_agent : str
        The User-Agent header.
    headers : dict of str to str, optional
        Further headers (e.g. ``Authorization``). urllib keeps them across a
        redirect, which the monitor's same-host 302 relies on.
    timeout : float, optional
        Seconds before a request is abandoned.

    Returns
    -------
    tuple of (int, bytes)
        The HTTP status and the body. A 4xx answer is returned, not raised.

    Raises
    ------
    urllib.error.URLError
        When both attempts fail with a 5xx, a timeout or a connection error.
    TimeoutError
        When both attempts time out while reading.
    """
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, **(headers or {})})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            if error.code < 500:
                return error.code, error.read()
            if attempt == 1:
                raise
            print(f"auto-scan: HTTP {error.code} from {url}, retrying", file=sys.stderr)
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == 1:
                raise
            print(f"auto-scan: {error} from {url}, retrying", file=sys.stderr)
        time.sleep(RETRY_DELAY_S)
    raise AssertionError("unreachable")
