import requests
import time
import os

# GitHub API configuration
GITHUB_API_URL = "https://api.github.com/repos/istio/istio/releases/latest"

# Azure DevOps configuration
AZURE_DEVOPS_ORG = "org"
AZURE_DEVOPS_PROJECT = "project"
AZURE_DEVOPS_PIPELINE_ID = "pipeline-id"
AZURE_DEVOPS_TOKEN = os.getenv("AZURE_DEVOPS_TOKEN")

# Teams webhook URL
TEAMS_WEBHOOK_URL = "https://outlook.office.com/webhook/teams-webhook-url"
SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/slack-webhook-url"

# Function to send a notification to Microsoft Teams
def notify_teams(message):
    headers = {
        "Content-Type": "application/json"
    }
    payload = {
        "text": message
    }
    response = requests.post(TEAMS_WEBHOOK_URL, json=payload, headers=headers)
    
    if response.status_code == 200:
        print(f"Notification sent to Teams successfully.")
    else:
        print(f"Failed to send notification to Teams: {response.status_code}, {response.text}")

# Function to get the latest Istio release from GitHub
def get_latest_istio_release():
    response = requests.get(GITHUB_API_URL)
    
    if response.status_code == 200:
        release_data = response.json()
        return release_data["tag_name"]  # Return the release tag (e.g., v1.11.0)
    else:
        print(f"Error fetching GitHub releases: {response.status_code}")
        return None

# Trigger Azure DevOps pipeline
def trigger_pipeline(release_tag):
    # Azure DevOps pipeline URL
    pipeline_url = f"https://dev.azure.com/{AZURE_DEVOPS_ORG}/{AZURE_DEVOPS_PROJECT}/_apis/pipelines/{AZURE_DEVOPS_PIPELINE_ID}/runs?api-version=6.0"
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {AZURE_DEVOPS_TOKEN}"
    }
    
    payload = {
        "resources": {
            "repositories": {
                "self": {
                    "refName": "refs/tags/" + release_tag
                }
            }
        }
    }
    
    response = requests.post(pipeline_url, json=payload, headers=headers)
    
    if response.status_code == 200:
        print(f"Pipeline triggered successfully for release: {release_tag}")
        notify_teams(f"Azure DevOps pipeline triggered successfully for Istio release: {release_tag}")
    else:
        print(f"Failed to trigger pipeline: {response.status_code}, {response.text}")
        notify_teams(f"Failed to trigger Azure DevOps pipeline for Istio release: {release_tag}.\nError: {response.status_code}, {response.text}")

# Main polling loop
def poll_for_releases():
    last_release = None  # To keep track of the last release we processed
    
    while True:
        print("Checking for new Istio releases...")
        latest_release = get_latest_istio_release()
        
        if latest_release and latest_release != last_release:
            print(f"New release detected: {latest_release}")
            notify_teams(f"New Istio release detected: {latest_release}")
            
            # Trigger the pipeline
            trigger_pipeline(latest_release)
            
            # Update the last release to avoid re-triggering for the same version
            last_release = latest_release
        
        # Wait 1h before checking again
        time.sleep(3600)

if __name__ == "__main__":
    poll_for_releases()
