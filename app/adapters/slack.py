from dataclasses import dataclass

import httpx

from app.core.config import settings


@dataclass(frozen=True)
class SlackUser:
    slack_user_id: str
    email: str
    name: str
    is_active: bool = True
    workspace_id: str | None = None


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

    def send_leave_request_prompt(self, channel_id: str, text: str) -> None:
        self._post_message(
            channel=channel_id,
            text=text,
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Request leave"},
                            "style": "primary",
                            "action_id": "open_leave_request_modal",
                            "value": "request_leave",
                        }
                    ],
                },
            ],
        )

    def send_employee_menu(self, channel_id: str, text: str) -> None:
        self._post_message(channel=channel_id, text=text, blocks=self._employee_action_blocks(text))

    def publish_employee_home(self, slack_user_id: str) -> None:
        text = (
            "*Leave bot*\n"
            "Request leave opens a form for your leave type and dates. "
            "Check balance shows approved days taken. View history shows your recent requests."
        )
        self._api(
            "views.publish",
            {
                "user_id": slack_user_id,
                "view": {
                    "type": "home",
                    "blocks": self._employee_action_blocks(text),
                },
            },
        )

    def open_leave_request_modal(self, trigger_id: str, leave_types: dict) -> None:
        options = [
            {
                "text": {"type": "plain_text", "text": rule.display_name},
                "value": key,
            }
            for key, rule in leave_types.items()
        ]
        self._api(
            "views.open",
            {
                "trigger_id": trigger_id,
                "view": {
                    "type": "modal",
                    "callback_id": "leave_request_submission",
                    "title": {"type": "plain_text", "text": "Request leave"},
                    "submit": {"type": "plain_text", "text": "Submit"},
                    "close": {"type": "plain_text", "text": "Cancel"},
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "leave_type",
                            "label": {"type": "plain_text", "text": "Leave type"},
                            "element": {
                                "type": "static_select",
                                "action_id": "leave_type_select",
                                "options": options,
                            },
                        },
                        {
                            "type": "input",
                            "block_id": "start_date",
                            "label": {"type": "plain_text", "text": "Start date"},
                            "element": {"type": "datepicker", "action_id": "start_date_select"},
                        },
                        {
                            "type": "input",
                            "block_id": "end_date",
                            "label": {"type": "plain_text", "text": "End date"},
                            "element": {"type": "datepicker", "action_id": "end_date_select"},
                        },
                        {
                            "type": "input",
                            "block_id": "reason",
                            "optional": True,
                            "label": {"type": "plain_text", "text": "Reason"},
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "reason_input",
                                "multiline": True,
                            },
                        },
                        {
                            "type": "input",
                            "block_id": "document",
                            "optional": True,
                            "label": {"type": "plain_text", "text": "Supporting document"},
                            "hint": {
                                "type": "plain_text",
                                "text": "Required only when the selected leave policy asks for proof. PDF, JPG, or PNG.",
                            },
                            "element": {
                                "type": "file_input",
                                "action_id": "document_input",
                                "filetypes": ["pdf", "jpg", "jpeg", "png"],
                                "max_files": 1,
                            },
                        },
                    ],
                },
            },
        )

    def send_approval_card(self, slack_user_id: str, leave_request_id: int, stage: str) -> None:
        self._post_message(
            channel=slack_user_id,
            text=f"Leave request #{leave_request_id} is waiting for {stage} approval.",
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Leave request #{leave_request_id}* is waiting for {stage} approval."},
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Approve"},
                            "style": "primary",
                            "action_id": "approve_leave",
                            "value": str(leave_request_id),
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Reject"},
                            "style": "danger",
                            "action_id": "reject_leave",
                            "value": str(leave_request_id),
                        },
                    ],
                },
            ],
        )

    def send_leave_approval(
        self,
        slack_user_id: str,
        request_id: int,
        employee_name: str,
        leave_type: str,
        start_date: str,
        end_date: str,
        days: float,
        document_url: str | None = None,
    ) -> None:
        summary = (
            f"*{employee_name}* requested *{leave_type} leave*\n"
            f"{start_date} to {end_date} | {days:g} day(s) | Request #{request_id}"
        )
        if document_url:
            summary += f"\n<{document_url}|View supporting document>"
        self._post_message(
            channel=slack_user_id,
            text=f"{employee_name} submitted leave request #{request_id}.",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Approve"},
                            "style": "primary",
                            "action_id": "approve_leave",
                            "value": str(request_id),
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Reject"},
                            "style": "danger",
                            "action_id": "reject_leave",
                            "value": str(request_id),
                        },
                    ],
                },
            ],
        )

    def list_users(self) -> list[SlackUser]:
        data = self._api("users.list", {})
        workspace_id = self._api("auth.test", {}).get("team_id")
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
                    workspace_id=workspace_id,
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

    def download_file(self, file_id: str) -> tuple[str, str, bytes]:
        file = self._api("files.info", {"file": file_id}).get("file") or {}
        size = int(file.get("size") or 0)
        if size > settings.document_max_bytes:
            raise ValueError(
                f"The document is too large. The current upload limit is "
                f"{settings.document_max_bytes // 1000} KB."
            )
        url = file.get("url_private_download") or file.get("url_private")
        if not url:
            raise RuntimeError("Slack did not provide a private download URL")
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=60,
            follow_redirects=True,
        )
        response.raise_for_status()
        return (
            file.get("name") or f"{file_id}.bin",
            file.get("mimetype") or response.headers.get("content-type", "").split(";")[0],
            response.content,
        )

    def _post_message(self, channel: str, text: str, blocks: list[dict] | None = None) -> None:
        payload = {"channel": channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        self._api("chat.postMessage", payload)

    @staticmethod
    def _employee_action_blocks(text: str) -> list[dict]:
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Request leave"},
                        "style": "primary",
                        "action_id": "open_leave_request_modal",
                        "value": "request_leave",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Check balance"},
                        "action_id": "check_leave_balance",
                        "value": "check_balance",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View history"},
                        "action_id": "view_leave_history",
                        "value": "leave_history",
                    },
                ],
            },
        ]

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
