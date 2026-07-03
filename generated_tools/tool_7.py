import os
import sys
import argparse
import requests

API_URL = "https://api.waqi.info/feed/{city}/?token={token}"
TIMEOUT = 10  # seconds

AQI_CATEGORIES = [
    (0, 50, "Good", "Air quality is satisfactory, and there is little or no risk."),
    (51, 100, "Moderate", "Sensitive individuals may experience minor health effects. General public is unlikely to be affected."),
    (101, 150, "Unhealthy for Sensitive Groups", "Sensitive groups (e.g., children, elderly, people with respiratory diseases) may experience health effects. General public is unlikely to be affected."),
    (151, 200, "Unhealthy", "Everyone may begin to experience health effects; members of sensitive groups may experience more serious effects."),
    (201, 300, "Very Unhealthy", "Health warnings of emergency conditions. The entire population is likely to be affected."),
    (301, 500, "Hazardous", "Health alert: everyone may experience more serious health effects."),
]

def get_category(aqi: int):
    for lo, hi, level, advice in AQI_CATEGORIES:
        if lo <= aqi <= hi:
            return level, advice
    return "Beyond Index", "AQI value exceeds the standard index range."

def fetch_aqi(city: str, token: str) -> dict:
    url = API_URL.format(city=city, token=token)
    try:
        resp = requests.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        sys.exit(f"Error contacting AQI service: {e}")
    data = resp.json()
    if data.get("status") != "ok":
        sys.exit(f"Failed to retrieve AQI data for '{city}'. Message: {data.get('data')}")
    return data["data"]

def main():
    parser = argparse.ArgumentParser(description="Retrieve current AQI for a city.")
    parser.add_argument("city", help="Name of the city (e.g., 'Los Angeles', 'Beijing')")
    args = parser.parse_args()

    token = os.getenv("AQI_API_TOKEN")
    if not token:
        sys.exit("Please set the environment variable AQI_API_TOKEN with your AQICN API token.")

    city = args.city.strip()
    aqi_data = fetch_aqi(city, token)

    aqi = aqi_data.get("aqi")
    if aqi is None or not isinstance(aqi, (int, float)):
        sys.exit(f"Invalid AQI data received for '{city}'.")

    level, advice = get_category(int(aqi))

    print(f"City: {city}")
    print(f"AQI: {aqi}")
    print(f"Category: {level}")
    print(f"Health Recommendation: {advice}")

if __name__ == "__main__":
    main()