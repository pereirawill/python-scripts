# This script is intended to run within an Azure DevOps pipeline to automate the review of pull requests (PRs) using Azure OpenAI. 
# It performs the following actions:
#
# - Fetches the Git diff for the current PR using subprocess.
# - Sends the diff to Azure OpenAI to generate a structured summary.
# - Posts the AI-generated summary back to the PR as a comment via the Azure DevOps API.
# - Uses environment variables for configuration, loaded through a dataclass.
#
# The summary includes:
# - Main changes
# - Breaking changes
# - Points of attention and suggested improvements
# - Potential impacts on the codebase or related systems

import os
import subprocess
import requests
import logging
from dataclasses import dataclass

# Set up logging configuration
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=log_level)
logger = logging.getLogger(__name__)

@dataclass
class Config:
    pr_id: str
    repo_id: str
    collection_uri: str
    project: str
    ado_token: str
    openai_api_key: str
    openai_endpoint: str
    openai_deployment: str = "gpt-4o-mini"
    openai_api_version: str = "2024-12-01-preview"
    
    def __post_init__(self):
        """Ensure all required environment variables are present."""
        for field in self.__dataclass_fields__:
            if not getattr(self, field):
                raise ValueError(f"Missing required environment variable: {field}")

def send_request(url: str, headers: dict, body: dict):
    """Helper function to send HTTP requests and handle errors."""
    try:
        response = requests.post(url, headers=headers, json=body)
        response.raise_for_status()  # Raise HTTPError for bad responses
        return response
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error: {e}")
        return None

def get_pr_diff():
    """Fetches the diff from the current PR against the main branch."""
    try:
        # Fetch the latest changes from the main branch
        fetch_result = subprocess.run(
            ["git", "fetch", "origin", "main"], 
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        
        # Get the diff between the current branch and main
        result = subprocess.run(
            ["git", "diff", "origin/main...HEAD"],
            capture_output=True, text=True, check=True
        )

        if result.stdout.strip():  # Check if there's any meaningful diff
            return result.stdout.strip()
        else:
            logger.info("No changes found in the PR.")
            return ""

    except subprocess.CalledProcessError as e:
        logger.error(f"Error getting Git diff: {e}\nStdout: {e.stdout}\nStderr: {e.stderr}")
        return ""
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return ""

def build_prompt(diff: str) -> str:
    return (
        "You are a senior code reviewer helping automate PR analysis.\n\n"
        "Analyze the following Git diff and provide a structured PR summary in markdown format.\n"
        "Be concise but specific. Avoid generic advice and tailor your insights to the changes shown.\n\n"
        "Your response must include the following sections:\n\n"
        "### Main Changes:\n"
        "- Summarize key changes, grouped by type (e.g., new features, bug fixes, refactorings).\n\n"
        "### Breaking Changes (if any):\n"
        "- Identify any breaking changes that could impact downstream systems, users, or dependent scripts.\n"
        "- Include changes to public APIs, interfaces, contracts, environment expectations, or configurations.\n\n"
        "### Points of Attention and Improvement Suggestions:\n"
        "- Suggest improvements in areas such as code quality, structure, naming, error handling, etc.\n\n"
        "### Potential Impacts on Codebase or Functionality:\n"
        "- Identify real, specific risks or side effects introduced by the changes.\n"
        "- Consider pipeline failures, backward compatibility issues, environment dependencies, or external service reliance.\n"
        "- Use bullet points, and explain why each item matters.\n\n"
        f"Git diff:\n{diff}"
    )

def call_azure_openai(prompt_text: str, config: Config):
    """Sends prompt to Azure OpenAI and returns the response text."""
    try:
        headers = {
            "Content-Type": "application/json",
            "api-key": config.openai_api_key
        }

        url = f"{config.openai_endpoint}/openai/deployments/{config.openai_deployment}/chat/completions?api-version={config.openai_api_version}"

        body = {
            "messages": [
                {"role": "system", "content": "You are a code review and software engineering expert."},
                {"role": "user", "content": prompt_text},
            ],
            "max_tokens": 1024,
            "temperature": 0.5,
            "top_p": 1.0
        }

        response = send_request(url, headers, body)
        
        if response:
            return response.json()["choices"][0]["message"]["content"].strip()
        return None
    except Exception as e:
        logger.error(f"Error calling Azure OpenAI: {str(e)}")
        return None

def post_azure_devops_comment(comment_text: str, config: Config):
    """Posts a comment back to the Azure DevOps Pull Request."""
    if not all([config.pr_id, config.repo_id, config.collection_uri, config.project]):
        logger.error("Missing required Azure DevOps environment variables.")
        return

    url = f"{config.collection_uri}{config.project}/_apis/git/repositories/{config.repo_id}/pullRequests/{config.pr_id}/threads?api-version=6.0"

    headers = {
        "Authorization": f"Bearer {config.ado_token}",
        "Content-Type": "application/json"
    }

    body = {
        "comments": [
            {"parentCommentId": 0, "content": comment_text, "commentType": 1}
        ],
        "status": 1
    }

    response = send_request(url, headers, body)
    
    if response and response.status_code in [200, 201]:
        logger.info("✅ Comment posted successfully!\n\n----- Comment Content -----\n%s\n---------------------------", comment_text.strip())
    else:
        logger.error("❌ Failed to post comment.\n\n----- Status Code -----\n%s\n\n----- Response Text -----\n%s\n------------------------", response.status_code, response.text.strip())

def main():
    try:
        config = Config(
            pr_id=os.environ.get("SYSTEM_PULLREQUEST_PULLREQUESTID"),
            repo_id=os.environ.get("BUILD_REPOSITORY_ID"),
            collection_uri=os.environ.get("SYSTEM_COLLECTIONURI"),
            project=os.environ.get("SYSTEM_TEAMPROJECT"),
            ado_token=os.environ.get("SYSTEM_ACCESSTOKEN"),
            openai_api_key=os.environ.get("AZURE_OPENAI_API_KEY"),
            openai_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT")
        )

        diff = get_pr_diff()
        if not diff:
            logger.info("No PR changes found.")
            return

        prompt_text = build_prompt(diff)
        summary = call_azure_openai(prompt_text, config)

        if summary:
            post_azure_devops_comment(f"### 🤖 Automated PR Summary\n\n{summary}", config)
        else:
            logger.warning("Could not generate summary.")
    
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")

if __name__ == "__main__":
    main()
