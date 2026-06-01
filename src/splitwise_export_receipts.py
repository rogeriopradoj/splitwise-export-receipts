import click
import os
import json
import requests
import pandas as pd
from requests_oauthlib import OAuth1
from splitwise import Splitwise
from splitwise.expense import Expense
from typing import Optional, List, Dict
import urllib.parse
import datetime

AUTH_FILE = os.path.expanduser("~/.splitwise_auth.json")

# --- Authentication ---
def get_request_token(consumer_key: str, consumer_secret: str, callback_url: str) -> tuple[str, str]:
    """Request an OAuth1 request token from Splitwise with an explicit callback."""
    oauth = OAuth1(consumer_key, client_secret=consumer_secret, callback_uri=callback_url)
    response = requests.post(Splitwise.REQUEST_TOKEN_URL, auth=oauth, timeout=20)
    response.raise_for_status()
    credentials = urllib.parse.parse_qs(response.text)
    if "oauth_token" not in credentials or "oauth_token_secret" not in credentials:
        raise ValueError("Splitwise did not return a valid request token.")
    return credentials["oauth_token"][0], credentials["oauth_token_secret"][0]


def save_auth_file(data: Dict[str, object]) -> None:
    with open(AUTH_FILE, 'w') as f:
        json.dump(data, f)
    os.chmod(AUTH_FILE, 0o600)


def authenticate() -> Splitwise:
    """
    Authenticate with Splitwise and return an API client object.
    Stores/reuses tokens in ~/.splitwise_auth.json.
    Guides user through app creation if needed.
    """
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, 'r') as f:
                data = json.load(f)
            consumer_key = data["consumer_key"]
            consumer_secret = data["consumer_secret"]
            auth_type = data.get("auth_type", "oauth1")
            sObj = Splitwise(consumer_key, consumer_secret)
            if auth_type == "oauth2":
                sObj.setOAuth2AccessToken(data["access_token"])
            else:
                sObj.setAccessToken({
                    "oauth_token": data["access_token"],
                    "oauth_token_secret": data["access_token_secret"]
                })
            return sObj
        except (KeyError, json.JSONDecodeError):
            print(f"Warning: {AUTH_FILE} is malformed or missing keys. Proceeding with interactive authentication.")

    print("\n--- Splitwise API Authentication ---")
    print("To use this tool, you need to create a Splitwise app to get your API keys.")
    print("1. Go to https://secure.splitwise.com/apps/new and create a new app.")
    print("2. Set the callback URL to anything (e.g., http://localhost:8080)")
    print("3. Copy your client ID / consumer key and secret below.\n")
    auth_type = click.prompt("Auth type", default="oauth2", type=click.Choice(["oauth2", "oauth1"], case_sensitive=False))
    consumer_key = click.prompt("Client ID / Consumer Key", type=str).strip()
    consumer_secret = click.prompt("Client Secret", type=str).strip()
    callback_url = click.prompt("Callback URL", default="http://localhost:8080", type=str).strip()

    sObj = Splitwise(consumer_key, consumer_secret)
    if auth_type == "oauth2":
        authorize_url, state = sObj.getOAuth2AuthorizeURL(callback_url)
        print(f"\nPlease visit this URL in your browser to authorize access:\n{authorize_url}\n")
        code = click.prompt("After authorizing, enter the authorization code (code)", type=str).strip()
        access_token = sObj.getOAuth2AccessToken(code, callback_url)
        if not access_token or "access_token" not in access_token:
            raise click.ClickException("Failed to obtain OAuth2 access token from Splitwise.")
        sObj.setOAuth2AccessToken(access_token)
        save_auth_file({
            "auth_type": "oauth2",
            "consumer_key": consumer_key,
            "consumer_secret": consumer_secret,
            "access_token": access_token
        })
    else:
        try:
            request_token, request_token_secret = get_request_token(consumer_key, consumer_secret, callback_url)
        except Exception as e:
            raise click.ClickException(f"Failed to get request token from Splitwise: {e}")

        authorize_url = f"https://secure.splitwise.com/authorize?oauth_token={request_token}"
        print(f"\nPlease visit this URL in your browser to authorize access:\n{authorize_url}\n")
        verifier = click.prompt("After authorizing, enter the verification code (oauth_verifier)", type=str).strip()
        access_token = sObj.getAccessToken(request_token, request_token_secret, verifier)
        sObj.setAccessToken(access_token)
        save_auth_file({
            "auth_type": "oauth1",
            "consumer_key": consumer_key,
            "consumer_secret": consumer_secret,
            "access_token": access_token["oauth_token"],
            "access_token_secret": access_token["oauth_token_secret"]
        })

    print(f"\nAuthentication successful! Credentials saved to {AUTH_FILE}\n")
    return sObj

# --- Expense Fetching ---
def fetch_expenses(client: Splitwise, group_id: Optional[int] = None, date_range: Optional[str] = None) -> List[Expense]:
    """
    Fetch all expenses for the user, optionally filtered by group or date.
    Handles pagination and returns a list of Expense objects.
    """
    expenses: List[Expense] = []
    offset = 0
    limit = 50  # Splitwise API max is 50 per call
    params = {}
    if group_id:
        params['group_id'] = group_id
    if date_range:
        try:
            start, end = date_range.split(":")
            # Validate ISO date format
            datetime.date.fromisoformat(start)
            datetime.date.fromisoformat(end)
            params['dated_after'] = start
            params['dated_before'] = end
        except Exception:
            print("Invalid date range format. Use YYYY-MM-DD:YYYY-MM-DD, and ensure both dates are valid ISO dates.")
            return []
    while True:
        batch = client.getExpenses(offset=offset, limit=limit, **params)
        if not batch:
            break
        expenses.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    print(f"Fetched {len(expenses)} expenses.")
    return expenses

# --- Receipt Downloading ---
def download_receipts(client: Splitwise, expenses: List[Expense], output_dir: str) -> Dict[int, str]:
    """
    Download all receipts for the given expenses to the output directory.
    Returns a mapping from expense ID to local receipt path (if downloaded).
    Shows a progress bar in the terminal.
    """
    os.makedirs(output_dir, exist_ok=True)
    receipt_map: Dict[int, str] = {}
    expenses_with_receipts = [exp for exp in expenses if getattr(getattr(exp, 'receipt', None), 'original', None)]
    session = requests.Session()
    if getattr(client, 'auth', None):
        session.auth = client.auth
    with click.progressbar(expenses_with_receipts, label="Downloading receipts", show_pos=True, show_percent=True) as bar:
        for exp in bar:
            receipt = getattr(exp, 'receipt', None)
            if receipt and getattr(receipt, 'original', None):
                url = receipt.original
                # Parse the URL to get the path without query string
                parsed_url = urllib.parse.urlparse(url)
                path = parsed_url.path
                ext = os.path.splitext(path)[-1] or '.jpg'
                local_name = f"receipt_{exp.id}{ext}"
                local_path = os.path.join(output_dir, local_name)
                try:
                    r = session.get(url, timeout=20, stream=True)
                    r.raise_for_status()
                    with open(local_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                    receipt_map[exp.id] = local_path
                except Exception as e:
                    print(f"Failed to download receipt for expense {exp.id}: {e}")
    print(f"Downloaded {len(receipt_map)} receipts.")
    return receipt_map

# --- Spreadsheet Export ---
def export_to_spreadsheet(expenses: List[Expense], receipt_map: Dict[int, str], output_file: str) -> None:
    """
    Export expenses and receipt links to a CSV/XLSX file.
    For CSV: the 'Receipt' column contains a clickable HYPERLINK formula for local files (works in Excel/Google Sheets).
    For XLSX: the 'Receipt' column contains the local file path as before.
    """
    rows = []
    for exp in expenses:
        receipt_path = receipt_map.get(exp.id, getattr(getattr(exp, 'receipt', None), 'original', ''))
        # For CSV, use Excel/Sheets HYPERLINK formula if a local file exists
        if output_file.lower().endswith('.csv') and receipt_path and os.path.exists(receipt_path):
            # Use file:// prefix for local files
            abs_path = os.path.abspath(receipt_path)
            receipt_cell = f'=HYPERLINK("file://{abs_path}", "View Receipt")'
            # Escape receipt_cell for CSV formula injection
            if receipt_cell and receipt_cell[0] in ('=', '+', '-', '@'):
                receipt_cell = "'" + receipt_cell
        elif output_file.lower().endswith('.csv'):
            receipt_cell = ''
        else:
            receipt_cell = receipt_path
        row = {
            'Expense ID': exp.id,
            'Group ID': getattr(exp, 'group_id', None),
            'Description': getattr(exp, 'description', ''),
            'Details': '',  # No separate details field; leave blank or map to another attribute if needed
            'Cost': getattr(exp, 'cost', ''),
            'Currency': getattr(exp, 'currency_code', ''),
            'Date': getattr(exp, 'date', ''),
            'Deleted': getattr(exp, 'deleted_at', None) is not None,
            'Deleted By': getattr(exp, 'deleted_by', None).getFirstName() if getattr(exp, 'deleted_by', None) else '',
            'Notes': getattr(exp, 'details', ''),
            'Receipt': receipt_cell,
        }
        rows.append(row)
    df = pd.DataFrame(rows)
    if output_file.lower().endswith('.csv'):
        df.to_csv(output_file, index=False)
    else:
        df.to_excel(output_file, index=False)
    print(f"Exported {len(rows)} expenses to {output_file}.")

@click.command()
@click.option('--output', '-o', default=None, help='Output spreadsheet file (CSV or XLSX)')
@click.option('--receipts-dir', default='receipts', help='Directory to save downloaded receipts')
@click.option('--group', default=None, type=int, help='Group ID to filter expenses')
@click.option('--date-range', default=None, help='Date range to filter expenses (e.g., 2023-01-01:2023-12-31)')
def main(output: Optional[str], receipts_dir: str, group: Optional[int], date_range: Optional[str]):
    """
    Export all Splitwise transactions and receipts to a spreadsheet.
    Prompts for output file if not provided.
    """
    client = authenticate()
    user = client.getCurrentUser()
    print(f"Authenticated as: {user.getFirstName()} {user.getLastName()} ({user.getEmail()})\n")
    expenses = fetch_expenses(client, group_id=group, date_range=date_range)
    receipt_map = download_receipts(client, expenses, receipts_dir)
    # Prompt for output file if not provided
    if not output:
        output = click.prompt("Enter output file path (CSV or XLSX)", default="splitwise_export.csv")
    export_to_spreadsheet(expenses, receipt_map, output)
    print(f"\nAll done! You can now open your exported file: {output}")
    print(f"Receipts (if any) are saved in: {os.path.abspath(receipts_dir)}")

if __name__ == '__main__':
    main() 