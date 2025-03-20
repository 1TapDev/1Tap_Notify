import requests

def check_for_updates(current_version, repo_url):
    try:
        response = requests.get(f"https://api.github.com/repos/{repo_url}/releases/latest")
        latest_version = response.json().get("tag_name", "Unknown")
        if latest_version != current_version:
            print(f"Update available: {latest_version}. Current version: {current_version}")
    except Exception as e:
        print(f"Error checking updates: {e}")
