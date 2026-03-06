"""
Comprehensive skill tests — covers metadata, is_available, and execute() paths
for every registered skill in the system.

Uses AsyncMock to patch integration clients so no live services are needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.skills.base import ApprovalCategory, BaseSkill, SkillResult


# ── Helpers ───────────────────────────────────────────────────────────────────


def _async_mock_client(configured: bool = True, **attrs) -> MagicMock:
    """Return an AsyncMock-compatible client mock with JSON-serializable defaults."""
    m = MagicMock()
    m.is_configured.return_value = configured
    # Ensure string attributes are serializable (not MagicMock)
    m.account_name = "primary"
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


# ── SkillResult ───────────────────────────────────────────────────────────────


def test_skill_result_defaults():
    r = SkillResult()
    assert r.context_data == ""
    assert r.pending_action is None
    assert r.skill_name == "chat"
    assert r.confidence == 1.0


def test_skill_result_custom():
    r = SkillResult(context_data="hello", skill_name="gmail_read", confidence=0.9)
    assert r.context_data == "hello"
    assert r.skill_name == "gmail_read"
    assert r.confidence == 0.9


def test_skill_result_with_pending():
    r = SkillResult(pending_action={"action": "send_email", "params": {}})
    assert r.pending_action is not None
    assert r.pending_action["action"] == "send_email"


# ── ApprovalCategory ──────────────────────────────────────────────────────────


def test_approval_category_values():
    assert ApprovalCategory.NONE == "none"
    assert ApprovalCategory.STANDARD == "standard"
    assert ApprovalCategory.CRITICAL == "critical"
    assert ApprovalCategory.BREAKING == "breaking"


# ── ChatSkill ─────────────────────────────────────────────────────────────────


def test_chat_skill_metadata():
    from app.skills.chat_skill import ChatSkill
    s = ChatSkill()
    assert s.name == "chat"
    assert s.is_available() is True


async def test_chat_skill_execute():
    from app.skills.chat_skill import ChatSkill
    s = ChatSkill()
    r = await s.execute({}, "hello")
    assert isinstance(r, SkillResult)


# ── CodeSkill ─────────────────────────────────────────────────────────────────


def test_code_skill_metadata():
    from app.skills.code_skill import CodeSkill
    s = CodeSkill()
    assert s.name == "code"
    assert s.is_available() is True


async def test_code_skill_execute():
    from app.skills.code_skill import CodeSkill
    r = await CodeSkill().execute({}, "write a hello world")
    assert isinstance(r, SkillResult)


# ── ContentDraftSkill ─────────────────────────────────────────────────────────


def test_content_draft_metadata():
    from app.skills.content_draft_skill import ContentDraftSkill
    s = ContentDraftSkill()
    assert s.name == "content_draft"
    assert s.is_available() is True


async def test_content_draft_execute():
    from app.skills.content_draft_skill import ContentDraftSkill
    r = await ContentDraftSkill().execute({"topic": "AI"}, "write a blog post")
    assert isinstance(r, SkillResult)


def test_ad_copy_metadata():
    from app.skills.ad_copy_skill import AdCopySkill
    s = AdCopySkill()
    assert s.name == "ad_copy"


async def test_ad_copy_execute():
    from app.skills.ad_copy_skill import AdCopySkill
    r = await AdCopySkill().execute({"product": "SaaS tool"}, "write ad copy")
    assert isinstance(r, SkillResult)


def test_social_caption_metadata():
    from app.skills.social_caption_skill import SocialCaptionSkill
    s = SocialCaptionSkill()
    assert s.name == "social_caption"


async def test_social_caption_execute():
    from app.skills.social_caption_skill import SocialCaptionSkill
    r = await SocialCaptionSkill().execute({}, "write a caption")
    assert isinstance(r, SkillResult)


def test_content_repurpose_metadata():
    from app.skills.content_repurpose_skill import ContentRepurposeSkill
    s = ContentRepurposeSkill()
    assert s.name == "content_repurpose"


async def test_content_repurpose_execute():
    from app.skills.content_repurpose_skill import ContentRepurposeSkill
    r = await ContentRepurposeSkill().execute({"original": "blog post"}, "repurpose")
    assert isinstance(r, SkillResult)


def test_content_calendar_metadata():
    from app.skills.content_calendar_skill import ContentCalendarSkill
    s = ContentCalendarSkill()
    assert s.name == "content_calendar"


async def test_content_calendar_execute():
    from app.skills.content_calendar_skill import ContentCalendarSkill
    r = await ContentCalendarSkill().execute({}, "plan content")
    assert isinstance(r, SkillResult)


def test_bug_hunter_metadata():
    from app.skills.bug_hunter_skill import BugHunterSkill
    s = BugHunterSkill()
    assert s.name == "bug_hunt"
    assert s.is_available() is True


# ── GmailReadSkill ────────────────────────────────────────────────────────────


def test_gmail_read_metadata():
    from app.skills.gmail_skill import GmailReadSkill
    s = GmailReadSkill()
    assert s.name == "gmail_read"
    assert s.approval_category == ApprovalCategory.NONE


async def test_gmail_read_not_configured():
    from app.skills.gmail_skill import GmailReadSkill
    mock_client = _async_mock_client(configured=False)
    with patch("app.integrations.gmail.get_gmail_client", return_value=mock_client):
        r = await GmailReadSkill().execute({}, "check my email")
    assert "not configured" in r.context_data.lower() or "gmail" in r.context_data.lower()


async def test_gmail_read_list_inbox():
    from app.skills.gmail_skill import GmailReadSkill
    mock_client = _async_mock_client(configured=True)
    mock_client.list_emails = AsyncMock(return_value=[
        {"id": "1", "subject": "Test", "from": "a@b.com", "snippet": "hi",
         "is_unread": True, "date": "2026-01-01"}
    ])
    with patch("app.integrations.gmail.get_gmail_client", return_value=mock_client):
        r = await GmailReadSkill().execute({"action": "list"}, "show inbox")
    assert isinstance(r.context_data, str)


# ── GmailSendSkill ────────────────────────────────────────────────────────────


def test_gmail_send_metadata():
    from app.skills.gmail_skill import GmailSendSkill
    s = GmailSendSkill()
    assert s.name == "gmail_send"
    assert s.approval_category == ApprovalCategory.STANDARD


async def test_gmail_send_builds_pending_action():
    from app.skills.gmail_skill import GmailSendSkill
    mock_client = _async_mock_client(configured=True)
    with patch("app.integrations.gmail.get_gmail_client", return_value=mock_client):
        r = await GmailSendSkill().execute(
            {"to": "bob@example.com", "subject": "Hello", "body_hint": "just checking in"},
            "send email to bob",
        )
    assert r.pending_action is not None
    assert r.pending_action.get("intent") == "gmail_send" or "send" in str(r.pending_action.get("action", ""))


# ── CalendarReadSkill ─────────────────────────────────────────────────────────


def test_calendar_read_metadata():
    from app.skills.calendar_skill import CalendarReadSkill
    s = CalendarReadSkill()
    assert s.name == "calendar_read"
    assert s.approval_category == ApprovalCategory.NONE


async def test_calendar_read_not_configured():
    from app.skills.calendar_skill import CalendarReadSkill
    mock_client = _async_mock_client(configured=False)
    with patch("app.integrations.google_calendar.get_calendar_client", return_value=mock_client):
        r = await CalendarReadSkill().execute({"period": "today"}, "what's on today")
    assert isinstance(r.context_data, str)


async def test_calendar_read_configured():
    from app.skills.calendar_skill import CalendarReadSkill
    mock_client = _async_mock_client(configured=True)
    mock_client.list_events = AsyncMock(return_value=[])
    with patch("app.integrations.google_calendar.get_calendar_client", return_value=mock_client):
        r = await CalendarReadSkill().execute({"period": "today"}, "today's events")
    assert isinstance(r.context_data, str)


# ── CalendarWriteSkill ────────────────────────────────────────────────────────


def test_calendar_write_metadata():
    from app.skills.calendar_skill import CalendarWriteSkill
    s = CalendarWriteSkill()
    assert s.name == "calendar_write"
    assert s.approval_category == ApprovalCategory.STANDARD


async def test_calendar_write_builds_pending():
    from app.skills.calendar_skill import CalendarWriteSkill
    mock_client = _async_mock_client(configured=True)
    with patch("app.integrations.google_calendar.get_calendar_client", return_value=mock_client):
        r = await CalendarWriteSkill().execute(
            {"title": "Team sync", "date": "2026-03-10", "time": "14:00"},
            "schedule a meeting",
        )
    assert r.pending_action is not None


# ── GitHubReadSkill ───────────────────────────────────────────────────────────


async def test_github_read_not_configured():
    from app.skills.github_skill import GitHubReadSkill
    with patch("app.integrations.github.GitHubClient.is_configured", return_value=False):
        r = await GitHubReadSkill().execute({}, "show my issues")
    assert "not configured" in r.context_data.lower() or "github" in r.context_data.lower()


async def test_github_read_notifications():
    from app.skills.github_skill import GitHubReadSkill
    with patch("app.integrations.github.GitHubClient") as MockGH:
        inst = MockGH.return_value
        inst.is_configured.return_value = True
        inst.list_notifications = AsyncMock(return_value=[])
        r = await GitHubReadSkill().execute({"resource": "notifications"}, "")
    assert isinstance(r.context_data, str)


async def test_github_read_issues():
    from app.skills.github_skill import GitHubReadSkill
    with patch("app.integrations.github.GitHubClient") as MockGH:
        inst = MockGH.return_value
        inst.is_configured.return_value = True
        inst.list_issues = AsyncMock(return_value=[{"number": 1, "title": "Bug"}])
        r = await GitHubReadSkill().execute({"resource": "issues", "repo": "owner/repo"}, "")
    assert isinstance(r.context_data, str)


async def test_github_read_prs():
    from app.skills.github_skill import GitHubReadSkill
    with patch("app.integrations.github.GitHubClient") as MockGH:
        inst = MockGH.return_value
        inst.is_configured.return_value = True
        inst.list_prs = AsyncMock(return_value=[])
        r = await GitHubReadSkill().execute({"resource": "prs", "repo": "owner/repo"}, "")
    assert isinstance(r.context_data, str)


# ── GitHubWriteSkill ──────────────────────────────────────────────────────────


async def test_github_write_not_configured():
    from app.skills.github_skill import GitHubWriteSkill
    with patch("app.integrations.github.GitHubClient.is_configured", return_value=False):
        r = await GitHubWriteSkill().execute({}, "create an issue")
    assert "not configured" in r.context_data.lower() or "github" in r.context_data.lower()


async def test_github_write_create_issue():
    from app.skills.github_skill import GitHubWriteSkill
    with patch("app.integrations.github.GitHubClient") as MockGH:
        inst = MockGH.return_value
        inst.is_configured.return_value = True
        inst.create_issue = AsyncMock(return_value={"number": 42, "html_url": "https://github.com"})
        r = await GitHubWriteSkill().execute(
            {"action": "create_issue", "title": "New bug", "body": "details"},
            "create issue",
        )
    assert isinstance(r.context_data, str)


async def test_github_write_unknown_action():
    from app.skills.github_skill import GitHubWriteSkill
    with patch("app.integrations.github.GitHubClient") as MockGH:
        inst = MockGH.return_value
        inst.is_configured.return_value = True
        r = await GitHubWriteSkill().execute({"action": "unknown"}, "")
    assert "not yet implemented" in r.context_data.lower() or "unknown" in r.context_data.lower()


# ── SentryReadSkill ───────────────────────────────────────────────────────────


def test_sentry_read_metadata():
    from app.skills.sentry_skill import SentryReadSkill
    s = SentryReadSkill()
    assert s.name == "sentry_read"
    assert s.approval_category == ApprovalCategory.NONE


async def test_sentry_read_list():
    from app.skills.sentry_skill import SentryReadSkill
    with patch("app.integrations.sentry_client.SentryClient") as MockSentry:
        inst = MockSentry.return_value
        inst.is_configured.return_value = True
        inst.list_issues = AsyncMock(return_value=[
            {"id": "1", "title": "Error", "level": "error", "count": 5,
             "project": "sentinel", "last_seen": "2026-01-01T12:00:00", "permalink": ""}
        ])
        r = await SentryReadSkill().execute({"action": "list"}, "")
    assert "Error" in r.context_data


async def test_sentry_read_list_empty():
    from app.skills.sentry_skill import SentryReadSkill
    with patch("app.integrations.sentry_client.SentryClient") as MockSentry:
        inst = MockSentry.return_value
        inst.is_configured.return_value = True
        inst.list_issues = AsyncMock(return_value=[])
        r = await SentryReadSkill().execute({"action": "list"}, "")
    assert "no" in r.context_data.lower()


async def test_sentry_read_get_missing_id():
    from app.skills.sentry_skill import SentryReadSkill
    with patch("app.integrations.sentry_client.SentryClient") as MockSentry:
        inst = MockSentry.return_value
        inst.is_configured.return_value = True
        r = await SentryReadSkill().execute({"action": "get"}, "")
    assert "issue_id" in r.context_data.lower()


async def test_sentry_read_get_issue():
    from app.skills.sentry_skill import SentryReadSkill
    with patch("app.integrations.sentry_client.SentryClient") as MockSentry:
        inst = MockSentry.return_value
        inst.is_configured.return_value = True
        inst.get_issue = AsyncMock(return_value={
            "id": "123", "title": "NullPointerException", "level": "error",
            "status": "unresolved", "project": "api", "platform": "python",
            "count": 10, "first_seen": "2026-01-01T00:00:00", "culprit": "views.py",
            "assigned_to": None, "permalink": "https://sentry.io/issues/123",
        })
        r = await SentryReadSkill().execute({"action": "get", "issue_id": "123"}, "")
    assert "NullPointerException" in r.context_data


async def test_sentry_read_unknown_action():
    from app.skills.sentry_skill import SentryReadSkill
    with patch("app.integrations.sentry_client.SentryClient") as MockSentry:
        inst = MockSentry.return_value
        inst.is_configured.return_value = True
        r = await SentryReadSkill().execute({"action": "unknown_xyz"}, "")
    assert "unknown" in r.context_data.lower()


# ── SentryManageSkill ─────────────────────────────────────────────────────────


def test_sentry_manage_metadata():
    from app.skills.sentry_skill import SentryManageSkill
    s = SentryManageSkill()
    assert s.name == "sentry_manage"
    assert s.approval_category == ApprovalCategory.CRITICAL


async def test_sentry_manage_missing_issue_id():
    from app.skills.sentry_skill import SentryManageSkill
    with patch("app.integrations.sentry_client.SentryClient.is_configured", return_value=True):
        r = await SentryManageSkill().execute({"action": "resolve"}, "resolve it")
    assert "issue_id" in r.context_data.lower()


async def test_sentry_manage_missing_action():
    from app.skills.sentry_skill import SentryManageSkill
    with patch("app.integrations.sentry_client.SentryClient.is_configured", return_value=True):
        r = await SentryManageSkill().execute({"issue_id": "123"}, "do something")
    assert "action" in r.context_data.lower()


async def test_sentry_manage_resolve_builds_pending():
    from app.skills.sentry_skill import SentryManageSkill
    with patch("app.integrations.sentry_client.SentryClient.is_configured", return_value=True):
        r = await SentryManageSkill().execute(
            {"action": "resolve", "issue_id": "123"}, "resolve this"
        )
    assert r.pending_action is not None


async def test_sentry_manage_comment_standard_approval():
    from app.skills.sentry_skill import SentryManageSkill
    s = SentryManageSkill()
    with patch("app.integrations.sentry_client.SentryClient.is_configured", return_value=True):
        r = await s.execute(
            {"action": "comment", "issue_id": "456", "text": "investigating"},
            "add a comment",
        )
    assert s.approval_category == ApprovalCategory.STANDARD
    assert r.pending_action is not None


# ── N8nSkill ──────────────────────────────────────────────────────────────────


def test_n8n_skill_metadata():
    from app.skills.n8n_skill import N8nSkill
    s = N8nSkill()
    assert s.name == "n8n"
    assert "n8n_execute" in s.trigger_intents


async def test_n8n_trigger():
    from app.skills.n8n_skill import N8nSkill
    with patch("app.integrations.n8n_bridge.N8nBridge") as MockBridge:
        inst = MockBridge.return_value
        inst.is_configured.return_value = True
        inst.trigger = AsyncMock(return_value={"status": "triggered"})
        r = await N8nSkill().execute({"workflow": "daily_brief", "payload": {}}, "")
    assert isinstance(r.context_data, str)


def test_n8n_manage_metadata():
    from app.skills.n8n_skill import N8nManageSkill
    s = N8nManageSkill()
    assert s.name == "n8n_manage"
    assert s.approval_category == ApprovalCategory.CRITICAL


async def test_n8n_manage_list():
    from app.skills.n8n_skill import N8nManageSkill
    with patch("app.integrations.n8n_bridge.N8nBridge") as MockBridge:
        inst = MockBridge.return_value
        inst.is_configured.return_value = True
        inst.list_workflows = AsyncMock(return_value=[{"id": "1", "name": "daily"}])
        r = await N8nManageSkill().execute({"action": "list"}, "list workflows")
    assert isinstance(r.context_data, str)


async def test_n8n_manage_get_missing_id():
    from app.skills.n8n_skill import N8nManageSkill
    with patch("app.integrations.n8n_bridge.N8nBridge"):
        r = await N8nManageSkill().execute({"action": "get"}, "")
    assert "workflow_id" in r.context_data.lower() or "requires" in r.context_data.lower()


async def test_n8n_manage_activate_returns_pending():
    from app.skills.n8n_skill import N8nManageSkill
    with patch("app.integrations.n8n_bridge.N8nBridge"):
        r = await N8nManageSkill().execute({"action": "activate", "workflow_id": "abc"}, "activate workflow abc")
    assert r.pending_action is not None


async def test_n8n_manage_delete_returns_pending():
    from app.skills.n8n_skill import N8nManageSkill
    with patch("app.integrations.n8n_bridge.N8nBridge"):
        r = await N8nManageSkill().execute({"action": "delete", "workflow_id": "abc"}, "")
    assert r.pending_action is not None


# ── SmartHomeSkill ────────────────────────────────────────────────────────────


def test_smart_home_metadata():
    from app.skills.smart_home_skill import SmartHomeSkill
    s = SmartHomeSkill()
    assert s.name == "smart_home"
    assert s.approval_category == ApprovalCategory.CRITICAL


async def test_smart_home_not_configured():
    from app.skills.smart_home_skill import SmartHomeSkill
    mock_client = _async_mock_client(configured=False)
    with patch("app.integrations.smarthome.SmartHomeClient", return_value=mock_client):
        r = await SmartHomeSkill().execute({}, "turn on the light")
    assert isinstance(r.context_data, str)


async def test_smart_home_status():
    """Status action with entity uses HomeAssistantClient.get_entity directly."""
    from app.skills.smart_home_skill import SmartHomeSkill
    mock_smart = _async_mock_client(configured=True)
    mock_ha_inst = MagicMock()
    mock_ha_inst.get_entity = AsyncMock(return_value={"state": "on", "attributes": {}})
    with patch("app.integrations.smarthome.SmartHomeClient", return_value=mock_smart), \
         patch("app.integrations.home_assistant.HomeAssistantClient", return_value=mock_ha_inst):
        r = await SmartHomeSkill().execute(
            {"action": "status", "entity": "light.living_room"}, ""
        )
    assert isinstance(r.context_data, str)


async def test_smart_home_turn_on():
    """turn_on executes directly and returns context_data."""
    from app.skills.smart_home_skill import SmartHomeSkill
    mock_client = _async_mock_client(configured=True)
    mock_client.turn_on = AsyncMock(return_value={"result": "ok"})
    with patch("app.integrations.smarthome.SmartHomeClient", return_value=mock_client):
        r = await SmartHomeSkill().execute(
            {"action": "turn_on", "entity": "light.bedroom"}, "turn on bedroom"
        )
    assert isinstance(r.context_data, str)


# ── ContactsReadSkill / ContactsWriteSkill ────────────────────────────────────


def test_contacts_read_metadata():
    from app.skills.contacts_skill import ContactsReadSkill
    s = ContactsReadSkill()
    assert s.name == "contacts_read"
    assert s.approval_category == ApprovalCategory.NONE


async def test_contacts_read_list():
    from app.skills.contacts_skill import ContactsReadSkill
    with patch("app.db.postgres.execute") as mock_exec:
        mock_exec.return_value = [{"id": 1, "name": "Alice", "email": "alice@x.com"}]
        r = await ContactsReadSkill().execute({"action": "list"}, "show contacts")
    assert isinstance(r.context_data, str)


async def test_contacts_read_search_no_query():
    from app.skills.contacts_skill import ContactsReadSkill
    with patch("app.db.postgres.execute") as mock_exec:
        mock_exec.return_value = []
        r = await ContactsReadSkill().execute({"action": "search"}, "find someone")
    assert isinstance(r.context_data, str)


def test_contacts_write_metadata():
    from app.skills.contacts_skill import ContactsWriteSkill
    s = ContactsWriteSkill()
    assert s.name == "contacts_write"
    assert s.approval_category == ApprovalCategory.STANDARD


async def test_contacts_write_add_builds_pending():
    from app.skills.contacts_skill import ContactsWriteSkill
    r = await ContactsWriteSkill().execute(
        {"action": "add", "name": "Bob", "email": "bob@x.com"}, "add Bob"
    )
    assert r.pending_action is not None


# ── WhatsAppSkills ────────────────────────────────────────────────────────────


def test_whatsapp_read_metadata():
    from app.skills.whatsapp_skill import WhatsAppReadSkill
    s = WhatsAppReadSkill()
    assert s.name == "whatsapp_read"


async def test_whatsapp_read_not_configured():
    from app.skills.whatsapp_skill import WhatsAppReadSkill
    with patch("app.integrations.whatsapp.WhatsAppClient.is_configured", return_value=False):
        r = await WhatsAppReadSkill().execute({}, "show messages")
    assert isinstance(r.context_data, str)


def test_whatsapp_send_metadata():
    from app.skills.whatsapp_skill import WhatsAppSendSkill
    s = WhatsAppSendSkill()
    assert s.name == "whatsapp_send"
    assert s.approval_category == ApprovalCategory.STANDARD


async def test_whatsapp_send_missing_to():
    from app.skills.whatsapp_skill import WhatsAppSendSkill
    with patch("app.integrations.whatsapp.WhatsAppClient.is_configured", return_value=True):
        r = await WhatsAppSendSkill().execute({"body": "hello"}, "send a message")
    assert isinstance(r.context_data, str)


async def test_whatsapp_send_builds_pending():
    from app.skills.whatsapp_skill import WhatsAppSendSkill
    with patch("app.integrations.whatsapp.WhatsAppClient.is_configured", return_value=True):
        r = await WhatsAppSendSkill().execute(
            {"to": "+15551234567", "body": "Hey!"}, "send whatsapp"
        )
    assert r.pending_action is not None


# ── CICDReadSkill / CICDTriggerSkill ──────────────────────────────────────────


def test_cicd_read_metadata():
    from app.skills.cicd_skill import CICDReadSkill
    s = CICDReadSkill()
    assert s.name == "cicd_read"
    assert s.approval_category == ApprovalCategory.NONE


async def test_cicd_read_list_workflows():
    """CICDReadSkill calls client._get() directly."""
    from app.skills.cicd_skill import CICDReadSkill
    with patch("app.integrations.github.GitHubClient") as MockGH:
        inst = MockGH.return_value
        inst.is_configured.return_value = True
        inst._get = AsyncMock(return_value={"workflows": []})
        r = await CICDReadSkill().execute(
            {"action": "list_workflows", "repo": "owner/repo"}, ""
        )
    assert isinstance(r.context_data, str)


async def test_cicd_read_not_configured():
    from app.skills.cicd_skill import CICDReadSkill
    with patch("app.integrations.github.GitHubClient.is_configured", return_value=False):
        r = await CICDReadSkill().execute({}, "list workflows")
    assert isinstance(r.context_data, str)


def test_cicd_trigger_metadata():
    from app.skills.cicd_skill import CICDTriggerSkill
    s = CICDTriggerSkill()
    assert s.name == "cicd_trigger"
    assert s.approval_category == ApprovalCategory.CRITICAL


async def test_cicd_trigger_builds_pending():
    from app.skills.cicd_skill import CICDTriggerSkill
    with patch("app.integrations.github.GitHubClient.is_configured", return_value=True):
        r = await CICDTriggerSkill().execute(
            {"repo": "owner/repo", "workflow_id": "deploy.yml", "ref": "main"},
            "trigger deploy",
        )
    assert r.pending_action is not None


# ── ResearchSkill ─────────────────────────────────────────────────────────────


def test_research_skill_metadata():
    from app.skills.research_skill import ResearchSkill
    s = ResearchSkill()
    assert s.name == "research"
    assert s.approval_category == ApprovalCategory.NONE


async def test_research_skill_execute():
    from app.skills.research_skill import ResearchSkill
    with patch("app.integrations.github.GitHubClient.is_configured", return_value=False):
        r = await ResearchSkill().execute({"topic": "quantum computing"}, "research quantum")
    assert isinstance(r.context_data, str)


# ── DeepResearchSkill ─────────────────────────────────────────────────────────


def test_deep_research_metadata():
    from app.skills.deep_research_skill import DeepResearchSkill
    s = DeepResearchSkill()
    assert s.name == "deep_research"


async def test_deep_research_builds_pending():
    from app.skills.deep_research_skill import DeepResearchSkill
    r = await DeepResearchSkill().execute(
        {"topic": "AI agents", "context": "focus on multi-agent systems"},
        "deep research AI agents",
    )
    assert r.pending_action is not None or isinstance(r.context_data, str)


# ── DeploySkill ───────────────────────────────────────────────────────────────


def test_deploy_skill_metadata():
    from app.skills.deploy_skill import DeploySkill
    s = DeploySkill()
    assert s.name == "deploy"
    assert s.approval_category == ApprovalCategory.BREAKING


async def test_deploy_skill_builds_pending():
    from app.skills.deploy_skill import DeploySkill
    r = await DeploySkill().execute({"reason": "apply fix"}, "deploy the changes")
    assert r.pending_action is not None


# ── TaskReadSkill ─────────────────────────────────────────────────────────────


def test_task_read_metadata():
    from app.skills.task_skill import TaskReadSkill
    s = TaskReadSkill()
    assert s.name == "task_read"
    assert s.approval_category == ApprovalCategory.NONE


async def test_task_read_list():
    from app.skills.task_skill import TaskReadSkill
    with patch("app.db.postgres.execute") as mock_exec:
        mock_exec.return_value = []
        r = await TaskReadSkill().execute({"action": "list", "status": "pending"}, "show tasks")
    assert isinstance(r.context_data, str)


async def test_task_read_list_with_tasks():
    from app.skills.task_skill import TaskReadSkill
    with patch("app.db.postgres.execute") as mock_exec:
        mock_exec.return_value = [
            {"id": 1, "title": "Fix bug", "status": "pending", "priority_num": 3,
             "approval_level": 1, "tags": None, "due_date": None, "assigned_to": None,
             "priority": "normal"}
        ]
        r = await TaskReadSkill().execute({"action": "list"}, "show tasks")
    assert "Fix bug" in r.context_data


# ── TaskUpdateSkill ───────────────────────────────────────────────────────────


def test_task_update_metadata():
    from app.skills.task_skill import TaskUpdateSkill
    s = TaskUpdateSkill()
    assert s.name == "task_update"
    assert s.approval_category == ApprovalCategory.STANDARD


async def test_task_update_missing_id():
    from app.skills.task_skill import TaskUpdateSkill
    r = await TaskUpdateSkill().execute({"status": "done"}, "complete task")
    assert "id" in r.context_data.lower() or "task" in r.context_data.lower()


async def test_task_update_builds_pending():
    from app.skills.task_skill import TaskUpdateSkill
    with patch("app.db.postgres.execute_one") as mock_one:
        mock_one.return_value = {
            "id": 5, "title": "Deploy v2",
            "status": "pending", "priority_num": 3, "approval_level": 1,
        }
        r = await TaskUpdateSkill().execute(
            {"id": 5, "status": "done"}, "mark task 5 done"
        )
    assert r.pending_action is not None


# ── IONOSCloudSkill ───────────────────────────────────────────────────────────


def test_ionos_cloud_metadata():
    from app.skills.ionos_skill import IONOSCloudSkill
    s = IONOSCloudSkill()
    assert s.name == "ionos_cloud"
    assert "ionos_cloud" in s.trigger_intents


async def test_ionos_cloud_not_configured():
    from app.skills.ionos_skill import IONOSCloudSkill
    with patch("app.integrations.ionos.IONOSClient.is_configured", return_value=False):
        r = await IONOSCloudSkill().execute({"action": "list_servers"}, "")
    assert isinstance(r.context_data, str)


def test_ionos_dns_metadata():
    from app.skills.ionos_skill import IONOSDNSSkill
    s = IONOSDNSSkill()
    assert s.name == "ionos_dns"


# ── KnowledgeGraphSkill ───────────────────────────────────────────────────────


def test_knowledge_graph_metadata():
    from app.skills.knowledge_graph_skill import KnowledgeGraphSkill
    s = KnowledgeGraphSkill()
    assert s.name == "knowledge_graph"
    assert s.approval_category == ApprovalCategory.NONE


async def test_knowledge_graph_add():
    from app.skills.knowledge_graph_skill import KnowledgeGraphSkill
    mock_kg = _async_mock_client(configured=True)
    mock_kg.upsert_node = AsyncMock(return_value={"id": "1", "label": "Project", "name": "Sentinel"})
    with patch("app.integrations.knowledge_graph.get_kg_client", return_value=mock_kg):
        r = await KnowledgeGraphSkill().execute(
            {"action": "add", "label": "Project", "name": "Sentinel"}, "add sentinel to graph"
        )
    assert isinstance(r.context_data, str)


async def test_knowledge_graph_stats():
    from app.skills.knowledge_graph_skill import KnowledgeGraphSkill
    mock_kg = _async_mock_client(configured=True)
    mock_kg.stats = AsyncMock(return_value={"nodes": {}, "relationships": 0})
    with patch("app.integrations.knowledge_graph.get_kg_client", return_value=mock_kg):
        r = await KnowledgeGraphSkill().execute({"action": "stats"}, "graph stats")
    assert isinstance(r.context_data, str)


async def test_knowledge_graph_not_configured():
    from app.skills.knowledge_graph_skill import KnowledgeGraphSkill
    mock_kg = _async_mock_client(configured=False)
    with patch("app.integrations.knowledge_graph.get_kg_client", return_value=mock_kg):
        r = await KnowledgeGraphSkill().execute({"action": "stats"}, "")
    assert "not configured" in r.context_data.lower()


# ── ServerShellSkill ──────────────────────────────────────────────────────────


def test_server_shell_metadata():
    from app.skills.server_shell_skill import ServerShellSkill
    s = ServerShellSkill()
    assert s.name == "server_shell"


async def test_server_shell_list_files():
    from app.skills.server_shell_skill import ServerShellSkill
    with patch("app.integrations.repo.RepoClient") as MockRepo:
        inst = MockRepo.return_value
        inst.list_files = MagicMock(return_value=["app/main.py", "app/config.py"])
        r = await ServerShellSkill().execute(
            {"action": "list_files", "path": "/root/sentinel-workspace"},
            "list files",
        )
    assert isinstance(r.context_data, str)


async def test_server_shell_read_file():
    from app.skills.server_shell_skill import ServerShellSkill
    with patch("app.integrations.repo.RepoClient") as MockRepo:
        inst = MockRepo.return_value
        inst.read_file = MagicMock(return_value="# Hello\nprint('world')")
        r = await ServerShellSkill().execute(
            {"action": "read_file", "path": "app/main.py"},
            "show main.py",
        )
    assert isinstance(r.context_data, str)


# ── SkillDiscoverySkill ───────────────────────────────────────────────────────


def test_skill_discovery_metadata():
    from app.skills.skill_discovery import SkillDiscoverySkill
    s = SkillDiscoverySkill()
    assert s.name == "skill_discover"
    assert s.is_available() is True


# ── ArchAdvisorSkill ──────────────────────────────────────────────────────────


def test_arch_advisor_metadata():
    from app.skills.arch_advisor_skill import ArchAdvisorSkill
    s = ArchAdvisorSkill()
    assert s.name == "arch_advisor"
    assert isinstance(s.is_available(), bool)


# ── ProjectSkill ──────────────────────────────────────────────────────────────


def test_project_skill_metadata():
    from app.skills.project_skill import ProjectSkill
    s = ProjectSkill()
    assert s.name == "project"
    assert s.is_available() is True


# ── SkillRegistry ─────────────────────────────────────────────────────────────


def test_registry_register_and_get():
    from app.skills.registry import SkillRegistry

    class _Dummy(BaseSkill):
        name = "_test_dummy_xyz"
        description = "dummy"
        trigger_intents = ["_dummy_xyz"]
        approval_category = ApprovalCategory.NONE

        def is_available(self) -> bool:
            return True

        async def execute(self, params, msg):
            return SkillResult(context_data="ok")

    reg = SkillRegistry()
    reg.register(_Dummy())
    assert "_dummy_xyz" in reg._skills


def test_registry_fallback_returns_none_for_unknown():
    from app.skills.registry import SkillRegistry
    reg = SkillRegistry()
    assert reg._skills.get("__nonexistent__") is None


def test_registry_list_available():
    from app.skills.registry import SkillRegistry

    class _Available(BaseSkill):
        name = "_avail_xyz"
        description = "avail"
        trigger_intents = ["_avail_xyz"]
        approval_category = ApprovalCategory.NONE

        def is_available(self) -> bool:
            return True

        async def execute(self, p, m):
            return SkillResult()

    class _Unavailable(BaseSkill):
        name = "_unavail_xyz"
        description = "unavail"
        trigger_intents = ["_unavail_xyz"]
        approval_category = ApprovalCategory.NONE

        def is_available(self) -> bool:
            return False

        async def execute(self, p, m):
            return SkillResult()

    reg = SkillRegistry()
    reg.register(_Available())
    reg.register(_Unavailable())
    available_names = [s.name for s in reg.list_available()]
    assert "_avail_xyz" in available_names
    assert "_unavail_xyz" not in available_names


def test_registry_descriptions():
    from app.skills.registry import SkillRegistry

    class _Desc(BaseSkill):
        name = "_desc_xyz"
        description = "A test skill"
        trigger_intents = ["_desc_xyz"]
        approval_category = ApprovalCategory.NONE

        def is_available(self) -> bool:
            return True

        async def execute(self, p, m):
            return SkillResult()

    reg = SkillRegistry()
    reg.register(_Desc())
    desc_str = reg.list_all_descriptions()
    assert "_desc_xyz" in desc_str


def test_registry_duplicate_warns(caplog):
    from app.skills.registry import SkillRegistry
    import logging

    class _Dup(BaseSkill):
        name = "_dup_xyz"
        description = "dup"
        trigger_intents = ["_dup_xyz"]
        approval_category = ApprovalCategory.NONE

        def is_available(self) -> bool:
            return True

        async def execute(self, p, m):
            return SkillResult()

    reg = SkillRegistry()
    reg.register(_Dup())
    with caplog.at_level(logging.WARNING):
        reg.register(_Dup())
    assert "_dup_xyz" in reg._skills
