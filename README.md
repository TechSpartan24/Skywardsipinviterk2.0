# Skywardsipinviterk2.0

A small Python utility that automates inviting your VRChat friends to a chosen
group while skipping people who are already members.

## Prerequisites

* Python 3.9+
* The `requests` library (`pip install requests`)
* A VRChat account with access to the target group

## Usage

1. Run the inviter with your desired group ID:

   ```bash
   python inviter.py <group_id>
   ```

2. Enter your VRChat username and password when prompted. If your account uses
   two-factor authentication (TOTP or email codes), the script will ask for the
   code and verify it before proceeding.

3. After authentication, the script will:

   * Authenticate with VRChat.
   * Pull the current group membership and write the user IDs to a temporary
     JSON file.
   * Fetch your friends list and write their user IDs to another temporary JSON
     file.
   * Compute the difference between the lists and invite only friends who are
     not already in the group.

4. When the script finishes it prints the locations of the two JSON files it
   created so you can inspect or archive them if desired.

> **Important**: This script calls the unofficial VRChat API and is provided for
> educational purposes. Use it responsibly and respect VRChat's Terms of
> Service. Inviting large numbers of users too quickly may result in rate
> limiting.

## Controlling request pacing

The script now throttles requests and automatically backs off when VRChat
signals that you are sending requests too quickly. You can fine-tune the
behaviour with the following optional environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `VRCHAT_REQUEST_DELAY` | `0.3` | Minimum delay (seconds) to wait between successful API requests. |
| `VRCHAT_BACKOFF_MULTIPLIER` | `2.0` | Exponential factor applied whenever a retry/backoff is needed. |
| `VRCHAT_MAX_BACKOFF` | `30` | Maximum delay (seconds) the backoff logic will wait between retries. |

Example:

```bash
export VRCHAT_REQUEST_DELAY=0.5
export VRCHAT_BACKOFF_MULTIPLIER=1.5
export VRCHAT_MAX_BACKOFF=20
```

If you do not set these variables the script falls back to the defaults shown
above.
