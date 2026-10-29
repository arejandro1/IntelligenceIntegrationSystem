import logging
import requests
from typing import Dict, Any
from urllib3.util import Retry
from requests.adapters import HTTPAdapter


# Configure logger
logger = logging.getLogger(__name__)


def common_post(url: str, data: Dict[str, Any], timeout: int = 10) -> Dict[str, Any]:
    """
    Send a JSON POST request with automatic retries and comprehensive error handling.

    Features:
    - Automatic JSON serialization and Content-Type header
    - Connection and read timeouts
    - Retry mechanism for server errors (5xx) and connection issues
    - Detailed request logging with UUID tracking
    - HTTP status code validation
    - Safe JSON response parsing

    :param url: Target URL endpoint
    :param data: JSON-serializable payload data (dictionary)
    :param timeout: Total timeout in seconds (connection + read)
    :return: Response JSON or error dictionary
    """
    # 安全与健壮性：连接/读超时拆分配置，timeout<=3 时读超时不为负
    connect_timeout = 3
    read_timeout = max(0.5, timeout - connect_timeout)

    # Configure retry strategy (3 retries with backoff)
    retry_strategy = Retry(
        total=3,
        backoff_factor=0.5,  # 0.5s → 1s → 2s intervals
        status_forcelist=[500, 502, 503, 504],  # Server errors
        allowed_methods=["POST"]  # Only retry on POST requests
    )

    # Create HTTP adapter with retry configuration
    adapter = HTTPAdapter(max_retries=retry_strategy)

    # Extract UUID for logging before any errors occur
    request_uuid = data.get('UUID', 'UNKNOWN_UUID')

    try:
        with requests.Session() as session:
            # Mount retry adapter to session
            session.mount("http://", adapter)
            session.mount("https://", adapter)

            # logger.info(f"Sending POST to {url} UUID={request_uuid}")

            # Send request with separate connection/read timeouts
            response = session.post(
                url,
                json=data,  # Auto-serializes to JSON + sets Content-Type
                headers={'X-Request-Source': 'IntelligenceHub'},
                timeout=(connect_timeout, read_timeout)
            )

            # Validate HTTP status (raises exception for 4xx/5xx)
            response.raise_for_status()

            # Attempt JSON parsing (handles empty/invalid responses)
            try:
                json_response = response.json()
                return json_response
            except ValueError as json_err:
                logger.error(f"JSON parse failed for {url} UUID={request_uuid}: {str(json_err)}")
                return {
                    "status": "error",
                    "uuid": request_uuid,
                    "reason": f"Invalid JSON response: {response.text[:100]}..."
                }

    except requests.exceptions.HTTPError as http_err:
        # Handle 4xx/5xx errors with response details
        status = http_err.response.status_code
        logger.error(f"HTTP error {status} at {url} UUID={request_uuid}: {str(http_err)}")
        return {
            "status": "error",
            "uuid": request_uuid,
            "reason": f"HTTP {status}: {http_err.response.text[:200]}..."
        }

    except requests.exceptions.ConnectionError as conn_err:
        # Network-level failures (DNS, refused connection)
        logger.error(f"Connection failed to {url} UUID={request_uuid}: {str(conn_err)}")
        return {
            "status": "error",
            "uuid": request_uuid,
            "reason": f"Network error: {str(conn_err)}"
        }

    except requests.exceptions.Timeout as timeout_err:
        # Handles both connect and read timeouts
        logger.error(f"Request timeout to {url} UUID={request_uuid}: {str(timeout_err)}")
        return {
            "status": "error",
            "uuid": request_uuid,
            "reason": f"Timeout after {timeout}s"
        }

    except requests.exceptions.RequestException as req_err:
        # Catch-all for other requests-related errors
        logger.error(f"Request failed to {url} UUID={request_uuid}: {str(req_err)}")
        return {
            "status": "error",
            "uuid": request_uuid,
            "reason": str(req_err)
        }

    except Exception as unexpected_err:
        # Handle non-requests exceptions (e.g., serialization)
        logger.exception(f"Unexpected error with {url} UUID={request_uuid}")
        return {
            "status": "error",
            "uuid": request_uuid,
            "reason": f"System error: {type(unexpected_err).__name__}"
        }
