import ssl
import gzip
import json
import time
import random
import http.client
import socket
import zlib
from io import BytesIO
from functools import wraps
from typing import Optional, Tuple
from urllib.parse import urlencode
from src import info, silent_error, error, RATE_LIMIT_INTERVAL, CF_IDENTIFIER, CF_API_TOKEN

class HTTPException(Exception):
    pass

def get_error_message(status: int, url: str) -> str:
    error_messages = {
        400: "400 Client Error: Bad Request",
        401: "401 Client Error: Unauthorized",
        403: "403 Client Error: Forbidden",
        404: "404 Client Error: Not Found",
        405: "405 Client Error: Method Not Allowed",
        406: "406 Client Error: Not Acceptable",
        408: "408 Client Error: Request Timeout",
        409: "409 Client Error: Conflict",
        410: "410 Client Error: Gone",
        411: "411 Client Error: Length Required",
        412: "412 Client Error: Precondition Failed",
        413: "413 Client Error: Payload Too Large",
        414: "414 Client Error: URI Too Long",
        415: "415 Client Error: Unsupported Media Type",
        416: "416 Client Error: Range Not Satisfiable",
        417: "417 Client Error: Expectation Failed",
        429: "429 Client Error: Too Many Requests",
        500: "500 Server Error: Internal Server Error",
        501: "501 Server Error: Not Implemented",
        502: "502 Server Error: Bad Gateway",
        503: "503 Server Error: Service Unavailable",
        504: "504 Server Error: Gateway Timeout",
        505: "505 Server Error: HTTP Version Not Supported"
    }
    if status in error_messages:
        return f"{error_messages[status]} for url: {url}"
    elif status >= 400 and status < 500:
        return f"{status} Client Error for url: {url}"
    elif status >= 500:
        return f"{status} Server Error for url: {url}"
    else:
        return f"HTTP request failed with status {status} for url: {url}"

def cloudflare_gateway_request(method: str, endpoint: str, body: Optional[str] = None, params: Optional[dict] = None, timeout: int = 10) -> Tuple[int, dict]:
    context = ssl.create_default_context()
    conn = http.client.HTTPSConnection("api.cloudflare.com", context=context, timeout=timeout)

    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip, deflate"
    }

    url = f"/client/v4/accounts/{CF_IDENTIFIER}/gateway{endpoint}"
    if params:
        url += f"?{urlencode(params)}"
    full_url = f"https://api.cloudflare.com{url}"

    try:
        conn.request(method, url, body, headers)
        response = conn.getresponse()
        data = response.read()
        status = response.status

        if status == 400:
            error_message = get_error_message(status, full_url)
            error(error_message)
            raise HTTPException(error_message)

        if status != 200:
            error_message = get_error_message(status, full_url)
            info(error_message)
            raise HTTPException(error_message)

        content_encoding = response.getheader('Content-Encoding')
        if content_encoding == 'gzip':
            buf = BytesIO(data)
            with gzip.GzipFile(fileobj=buf) as f:
                data = f.read()
        elif content_encoding == 'deflate':
            data = zlib.decompress(data)

        return status, json.loads(data.decode('utf-8'))

    except (http.client.HTTPException, ssl.SSLError, socket.timeout, OSError) as e:
        info(f"Network error occurred: {e}")
        raise HTTPException(f"Network error occurred: {e}")
    except json.JSONDecodeError:
        info("Failed to decode JSON response")
        raise HTTPException("Failed to decode JSON response")
    finally:
        conn.close()

def stop_never(attempt_number):
    return False

def wait_random_exponential(attempt_number, multiplier=1, max_wait=10):
    return min(multiplier * (2 ** random.uniform(0, attempt_number - 1)), max_wait)

def retry_if_exception_type(exceptions):
    return lambda e: isinstance(e, exceptions)

def retry(stop=None, wait=None, retry=None, after=None, before_sleep=None):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempt_number = 0
            while True:
                try:
                    attempt_number += 1
                    return func(*args, **kwargs)
                except Exception as e:
                    if retry and not retry(e):
                        raise
                    if after:
                        after({'attempt_number': attempt_number, 'outcome': e})
                    if stop and stop(attempt_number):
                        raise
                    if before_sleep:
                        before_sleep({'attempt_number': attempt_number})
                    wait_time = wait(attempt_number) if wait else 1
                    time.sleep(wait_time)
        return wrapper
    return decorator

retry_config = {
    'stop': stop_never,
    'wait': lambda attempt_number: wait_random_exponential(
        attempt_number, multiplier=1, max_wait=10
    ),
    'retry': retry_if_exception_type((HTTPException,)),
    'after': lambda retry_state: info(
        f"Retrying ({retry_state['attempt_number']}): {retry_state['outcome']}"
    ),
    'before_sleep': lambda retry_state: info(
        f"Sleeping before next retry ({retry_state['attempt_number']})"
    )
}

class RateLimiter:
    def __init__(self, interval):
        self.interval = interval
        self.timestamp = time.time()

    def wait_for_next_request(self):
        now = time.time()
        elapsed = now - self.timestamp
        sleep_time = max(0, self.interval - elapsed)
        if sleep_time > 0:
            time.sleep(sleep_time)
        self.timestamp = time.time()

rate_limiter = RateLimiter(RATE_LIMIT_INTERVAL)

def rate_limited_request(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        rate_limiter.wait_for_next_request()
        return func(*args, **kwargs)
    return wrapper