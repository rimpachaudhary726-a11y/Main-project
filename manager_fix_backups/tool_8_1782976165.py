#!/usr/bin/env python3
import sys
import json
import traceback

def fetch_top_headlines(url, count=5):
    try:
        import requests
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
    except ImportError:
        # Fallback to urllib if requests is unavailable
        from urllib.request import urlopen, Request
        from urllib.error import URLError, HTTPError
        try:
            req = Request(url, headers={'User-Agent': 'python'})
            with urlopen(req, timeout=10) as f:
                raw = f.read().decode('utf-8')
            data = json.loads(raw)
        except (URLError, HTTPError) as e:
            raise RuntimeError(f"Network error: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Failed to fetch data: {e}") from e

    if not isinstance(data, dict) or 'hits' not in data:
        raise ValueError("Unexpected response format")
    hits = data['hits']
    headlines = []
    for item in hits[:count]:
        title = item.get('title') or item.get('story_title') or item.get('story_text') or ''
        if title:
            headlines.append(title.strip())
    return headlines

def main():
    API_URL = "https://hn.algolia.com/api/v1/search?tags=front_page"
    try:
        headlines = fetch_top_headlines(API_URL, count=5)
        if not headlines:
            print("No headlines found.")
            return
        print("Top 5 Latest News Headlines:")
        for idx, title in enumerate(headlines, 1):
            print(f"{idx}. {title}")
    except Exception:
        print("An error occurred while retrieving headlines:", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()