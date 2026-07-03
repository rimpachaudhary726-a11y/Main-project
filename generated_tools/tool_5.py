import os
import sys
import json
import socket
import urllib.request
import urllib.error

# Configuration
DEFAULT_API_URL = "https://wttr.in/London?format=j1"
API_URL_ENV = "WEATHER_API_URL"
TIMEOUT_SECONDS = 10

def get_api_url() -> str:
    """
    Retrieve the weather API URL from the environment variable if set,
    otherwise fall back to the default URL.
    """
    url = os.getenv(API_URL_ENV, "").strip()
    if url:
        return url
    return DEFAULT_API_URL

def fetch_weather(url: str) -> dict:
    """
    Fetch weather data from the given URL.
    Returns a dictionary with the parsed JSON response.
    Raises RuntimeError with a descriptive message on failure.
    """
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise RuntimeError(f"Unexpected HTTP status: {response.status}")
            content_type = response.headers.get("Content-Type", "")
            if "application/json" not in content_type and "text/plain" not in content_type:
                raise RuntimeError(f"Unexpected Content-Type: {content_type}")
            raw_data = response.read()
            try:
                return json.loads(raw_data.decode("utf-8"))
            except json.JSONDecodeError as e:
                raise RuntimeError(f"Failed to parse JSON: {e}") from e
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP error: {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, socket.gaierror):
            raise RuntimeError("DNS resolution failed") from e
        raise RuntimeError(f"URL error: {e.reason}") from e
    except socket.timeout:
        raise RuntimeError("Connection timed out")
    except Exception as e:
        raise RuntimeError(f"Unexpected error: {e}") from e

def format_weather(data: dict) -> str:
    """
    Transform the raw API response into a human‑readable string.
    Handles the wttr.in JSON structure.
    """
    try:
        # wttr.in returns a list of weather entries under 'current_condition'
        current = data["current_condition"][0]
        temp_c = current.get("temp_C", "N/A")
        feels_like_c = current.get("FeelsLikeC", "N/A")
        weather_desc = current.get("weatherDesc", [{}])[0].get("value", "N/A")
        humidity = current.get("humidity", "N/A")
        wind_kph = current.get("windspeedKmph", "N/A")
        return (
            f"Current weather in London:\n"
            f"  Temperature: {temp_c}°C (feels like {feels_like_c}°C)\n"
            f"  Condition: {weather_desc}\n"
            f"  Humidity: {humidity}%\n"
            f"  Wind Speed: {wind_kph} km/h"
        )
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Unexpected data format: {e}") from e

def main():
    # Verify environment variables (if defined)
    if API_URL_ENV in os.environ:
        url = os.getenv(API_URL_ENV)
        if not url:
            sys.stderr.write(f"Error: Environment variable {API_URL_ENV} is set but empty.\n")
            sys.exit(1)
    else:
        url = DEFAULT_API_URL

    # Confirm that the endpoint looks like a URL
    if not (url.startswith("http://") or url.startswith("https://")):
        sys.stderr.write(f"Error: Invalid URL in {API_URL_ENV}: {url}\n")
        sys.exit(1)

    try:
        weather_data = fetch_weather(url)
        output = format_weather(weather_data)
        print(output)
    except RuntimeError as e:
        sys.stderr.write(f"Failed to retrieve weather: {e}\n")
        sys.exit(1)

if __name__ == "__main__":
    main()