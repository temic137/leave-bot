from dataclasses import dataclass

import httpx

from app.core.config import settings


@dataclass(frozen=True)
class SlackUser:
    slack_user_id: str
    email: str
    name: str
    is_active: bool = True


class SlackClient:
    def send_message(self, slack_user_id: str, text: str) -> None:
        raise NotImplementedError

    def send_approval_card(self, slack_user_id: str, leave_request_id: int, stage: str) -> None:
        raise NotImplementedError

    def list_users(self) -> list[SlackUser]:
        raise NotImplementedError


class ConsoleSlackClient(SlackClient):
    def send_message(self, slack_user_id: str, text: str) -> None:
        print(f"[slack message] to={slack_user_id} text={text}")

    def send_approval_card(self, slack_user_id: str, leave_request_id: int, stage: str) -> None:
        print(f"[slack approval] to={slack_user_id} request={leave_request_id} stage={stage}")

    def list_users(self) -> list[SlackUser]:
        return [
            SlackUser(slack_user_id="U_ADA", email="ada@example.com", name="Ada Example"),
            SlackUser(slack_user_id="U_BAYO", email="bayo@example.com", name="Bayo Example"),
            SlackUser(slack_user_id="U_CHIOMA", email="chioma@example.com", name="Chioma Example"),
            SlackUser(slack_user_id="U_DANIEL", email="daniel@example.com", name="Daniel Example"),
            SlackUser(slack_user_id="U_JAMES", email="james@example.com", name="James Example"),
        ]


class RealSlackClient(SlackClient):
    def __init__(self, token: str = settings.slack_bot_token):
        self.token = token

    def send_message(self, slack_user_id: str, text: str) -> None:
        self._post_message(channel=slack_user_id, text=text)

    def send_channel_message(self, channel_id: str, text: str) -> None:
        self._post_message(channel=channel_id, text=text)

    def send_approval_card(self, slack_user_id: str, leave_request_id: int, stage: str) -> None:
        self.send_message(
            slack_user_id,
            f"Leave request #{leave_request_id} is waiting for {stage} approval. Reply `approve request {leave_request_id}` or `reject request {leave_request_id}`.",
        )

    def list_users(self) -> list[SlackUser]:
        data = self._api("users.list", {})
        users = []
        for member in data.get("members", []):
            profile = member.get("profile", {})
            email = profile.get("email")
            if member.get("is_bot") or member.get("deleted") or not email:
                continue
            users.append(
                SlackUser(
                    slack_user_id=member["id"],
                    email=email,
                    name=profile.get("real_name") or member.get("real_name") or member.get("name") or email,
                    is_active=not member.get("deleted", False),
                )
            )
        return users

    def list_user_directory(self) -> list[dict]:
        data = self._api("users.list", {})
        directory = []
        for member in data.get("members", []):
            if member.get("is_bot") or member.get("deleted"):
                continue
            profile = member.get("profile", {})
            directory.append(
                {
                    "slack_user_id": member["id"],
                    "name": profile.get("real_name") or member.get("real_name") or member.get("name") or member["id"],
                    "email": profile.get("email"),
                }
            )
        return directory

    def _post_message(self, channel: str, text: str) -> None:
        self._api("chat.postMessage", {"channel": channel, "text": text})

    def _api(self, method: str, payload: dict) -> dict:
        if not self.token:
            raise RuntimeError("SLACK_BOT_TOKEN is not configured")
        response = httpx.post(
            f"https://slack.com/api/{method}",
            headers={"Authorization": f"Bearer {self.token}"},
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack API {method} failed: {data.get('error')}")
        return data
