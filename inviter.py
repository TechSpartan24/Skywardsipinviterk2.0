"""Automate inviting VRChat friends to a group.

This script logs into the VRChat API, collects existing group members, compares
against the current friends list, and sends invitations to friends that are not
already members. Temporary JSON files are produced along the way to satisfy the
requested workflow.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

import requests

API_ROOT = "https://api.vrchat.cloud/api/1"
DEFAULT_PAGE_SIZE = 100
REQUEST_TIMEOUT = 30


def _load_float_env(name: str, default: float) -> float:
    """Return a float from the environment, falling back to ``default``."""

    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError:
        print(
            f"Warning: ignoring invalid value for {name!s}={raw_value!r}; using {default}."
        )
        return default


REQUEST_DELAY = _load_float_env("VRCHAT_REQUEST_DELAY", 0.3)
BACKOFF_MULTIPLIER = max(_load_float_env("VRCHAT_BACKOFF_MULTIPLIER", 2.0), 1.0)
MAX_BACKOFF_DELAY = max(_load_float_env("VRCHAT_MAX_BACKOFF", 30.0), REQUEST_DELAY)
MAX_RETRY_ATTEMPTS = 5


class DelayController:
    """Coordinate pacing and exponential backoff for VRChat API calls."""

    def __init__(
        self,
        base_delay: float,
        multiplier: float,
        max_delay: float,
    ) -> None:
        self.base_delay = max(base_delay, 0.0)
        self.multiplier = max(multiplier, 1.0)
        self.max_delay = max(max_delay, self.base_delay)
        self._backoff_delay = self.base_delay if self.base_delay > 0 else 1.0
        self._next_request_time = time.monotonic()

    def wait_for_turn(self) -> None:
        """Sleep if necessary so requests are spaced out."""

        if self.base_delay <= 0:
            return
        now = time.monotonic()
        if now < self._next_request_time:
            wait_time = self._next_request_time - now
            print(f"Waiting {wait_time:.2f}s to respect API pacing…")
            time.sleep(wait_time)

    def note_success(self) -> None:
        """Reset backoff tracking after a successful request."""

        self._backoff_delay = self.base_delay if self.base_delay > 0 else 1.0
        self._next_request_time = time.monotonic() + self.base_delay

    def backoff(self, *, minimum: float = 0.0, reason: Optional[str] = None) -> None:
        """Sleep using exponential backoff, optionally logging the reason."""

        wait_time = max(minimum, self._backoff_delay)
        if wait_time > 0:
            if reason:
                print(f"{reason} Waiting {wait_time:.2f}s before retrying…")
            else:
                print(f"Waiting {wait_time:.2f}s before retrying…")
            time.sleep(wait_time)
        next_backoff = self._backoff_delay * self.multiplier
        if self.base_delay > 0:
            next_backoff = max(next_backoff, self.base_delay)
        self._backoff_delay = min(next_backoff, self.max_delay)
        self._next_request_time = time.monotonic() + self.base_delay


delay_controller = DelayController(REQUEST_DELAY, BACKOFF_MULTIPLIER, MAX_BACKOFF_DELAY)


class VRChatError(RuntimeError):
    """Represents an error received from the VRChat API."""


@dataclass
class Credentials:
    username: str
    password: str
    two_factor: Optional[str] = None

def load_credentials() -> Credentials:
    """Load credentials from environment variables or prompt the user."""

    username = os.getenv("VRCHAT_USERNAME") or input("VRChat username: ")
    password = os.getenv("VRCHAT_PASSWORD") or input("VRChat password: ")
    two_factor = os.getenv("VRCHAT_2FA") or input("2FA/OTP (press enter if none): ")
    if not two_factor:
        two_factor = None
    return Credentials(username=username, password=password, two_factor=two_factor)


def create_session(creds: Credentials) -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = "skywards-auto-inviter/2.0"
    session.auth = (creds.username, creds.password)
    if creds.two_factor:
        session.headers["X-Auth-Authorization"] = creds.two_factor
    return session


def _retry_after_seconds(response: requests.Response) -> float:
    header = response.headers.get("Retry-After")
    if not header:
        return 0.0
    try:
        return float(header)
    except ValueError:
        return 0.0


def perform_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    retries: int = MAX_RETRY_ATTEMPTS,
    **kwargs,
) -> requests.Response:
    attempts = 0
    while True:
        delay_controller.wait_for_turn()
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        try:
            response = session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            attempts += 1
            if attempts > retries:
                raise VRChatError(f"Request failed after {retries} retries: {exc}") from exc
            delay_controller.backoff(reason=f"Network error: {exc}")
            continue

        if response.status_code == 429:
            delay_controller.backoff(
                minimum=_retry_after_seconds(response),
                reason="Rate limited by VRChat API.",
            )
            continue

        if response.status_code >= 500 and attempts < retries:
            attempts += 1
            delay_controller.backoff(
                reason=f"Server error {response.status_code}.",
            )
            continue

        delay_controller.note_success()
        return response


def _handle_response(response: requests.Response) -> dict:
    if response.status_code >= 400:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise VRChatError(f"API error {response.status_code}: {detail}")
    if not response.content:
        return {}
    return response.json()


def get_current_user(session: requests.Session) -> dict:
    resp = perform_request(session, "GET", f"{API_ROOT}/auth/user")
    data = _handle_response(resp)
    if "username" not in data:
        raise VRChatError("Unable to authenticate with supplied credentials.")
    return data


def get_group(session: requests.Session, group_id: str) -> dict:
    resp = perform_request(session, "GET", f"{API_ROOT}/groups/{group_id}")
    return _handle_response(resp)


def get_group_members(session: requests.Session, group_id: str) -> List[dict]:
    members: List[dict] = []
    offset = 0
    while True:
        params = {"n": DEFAULT_PAGE_SIZE, "offset": offset}
        resp = perform_request(
            session,
            "GET",
            f"{API_ROOT}/groups/{group_id}/members",
            params=params,
        )
        batch = _handle_response(resp)
        if not isinstance(batch, list):
            raise VRChatError("Unexpected member list format.")
        members.extend(batch)
        if len(batch) < DEFAULT_PAGE_SIZE:
            break
        offset += len(batch)
    return members


def get_friends(session: requests.Session) -> List[dict]:
    offset = 0
    friends: List[dict] = []
    while True:
        params = {"n": DEFAULT_PAGE_SIZE, "offset": offset}
        resp = perform_request(
            session,
            "GET",
            f"{API_ROOT}/auth/user/friends",
            params=params,
        )
        batch = _handle_response(resp)
        if not isinstance(batch, list):
            raise VRChatError("Unexpected friends list format.")
        friends.extend(batch)
        if len(batch) < DEFAULT_PAGE_SIZE:
            break
        offset += len(batch)
    return friends


def write_temp_json(records: Sequence[dict], key: str, label: str) -> str:
    data = [record.get(key) for record in records if record.get(key)]
    temp_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=f"_{label}.json", delete=False, encoding="utf-8"
    )
    json.dump(data, temp_file, indent=2)
    temp_file.close()
    print(f"Created temporary {label} list at {temp_file.name} ({len(data)} entries)")
    return temp_file.name


def invite_users(
    session: requests.Session, group_id: str, user_ids: Iterable[str]
) -> None:
    for user_id in user_ids:
        payload = {"userId": user_id}
        resp = perform_request(
            session,
            "POST",
            f"{API_ROOT}/groups/{group_id}/invites",
            json=payload,
        )
        try:
            _handle_response(resp)
        except VRChatError as exc:
            print(f"Failed to invite {user_id}: {exc}")
        else:
            print(f"Invited {user_id} to group {group_id}")


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        print("Usage: python inviter.py <group_id>")
        return 2

    group_id = argv[1]
    creds = load_credentials()
    session = create_session(creds)

    print("Authenticating with VRChat API…")
    user = get_current_user(session)
    print(f"Authenticated as {user['displayName']} ({user['id']})")

    print("Fetching group details…")
    group = get_group(session, group_id)
    print(f"Group: {group.get('name', group_id)}")

    print("Fetching group members…")
    members = get_group_members(session, group_id)
    member_ids = {member.get("userId") or member.get("id") for member in members}
    group_file = write_temp_json(members, "userId", "group_members")

    print("Fetching friends list…")
    friends = get_friends(session)
    friend_ids = {friend.get("id") for friend in friends if friend.get("id")}
    friends_file = write_temp_json(friends, "id", "friends")

    print("Calculating invite candidates…")
    invite_candidates = sorted(friend_ids - member_ids)
    print(
        f"{len(invite_candidates)} friends are not in the group and will be invited."
    )

    if not invite_candidates:
        print("No invitations needed!")
        return 0

    print("Inviting friends…")
    invite_users(session, group_id, invite_candidates)

    print("Process complete!")
    print(f"Group members saved to: {group_file}")
    print(f"Friends saved to: {friends_file}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except VRChatError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)
