"""Unit tests for app.router.task_board helpers and Pydantic models."""
import pytest
from app.router.task_board import (
    TaskCreate,
    TaskUpdate,
    _enrich,
    _PRIORITY_LABEL,
    _PRIORITY_TO_TEXT,
    _APPROVAL_LABEL,
)


# ── _enrich() ─────────────────────────────────────────────────────────────────

def test_enrich_adds_priority_label():
    row = {"id": 1, "title": "Fix bug", "priority_num": 5, "approval_level": 1}
    result = _enrich(row)
    assert result["priority_label"] == "Critical"
    assert result["approval_label"] == "auto-approve"


def test_enrich_all_priority_levels():
    expected = {1: "Low", 2: "Minor", 3: "Normal", 4: "High", 5: "Critical"}
    for num, label in expected.items():
        row = {"priority_num": num, "approval_level": 1}
        assert _enrich(row)["priority_label"] == label


def test_enrich_all_approval_levels():
    expected = {1: "auto-approve", 2: "needs review", 3: "requires sign-off"}
    for num, label in expected.items():
        row = {"priority_num": 3, "approval_level": num}
        assert _enrich(row)["approval_label"] == label


def test_enrich_defaults_when_none():
    """None values fall back to priority_num=3, approval_level=2."""
    row = {"id": 42, "priority_num": None, "approval_level": None}
    result = _enrich(row)
    assert result["priority_label"] == "Normal"
    assert result["approval_label"] == "needs review"


def test_enrich_preserves_all_original_fields():
    row = {
        "id": 7,
        "title": "Deploy feature",
        "description": "Ship it",
        "status": "pending",
        "priority_num": 4,
        "approval_level": 2,
    }
    result = _enrich(row)
    assert result["id"] == 7
    assert result["title"] == "Deploy feature"
    assert result["status"] == "pending"


def test_enrich_does_not_mutate_input():
    row = {"priority_num": 3, "approval_level": 1}
    original = dict(row)
    _enrich(row)
    assert row == original


# ── Priority / approval label maps ────────────────────────────────────────────

def test_priority_to_text_covers_all_levels():
    for level in range(1, 6):
        assert level in _PRIORITY_TO_TEXT
        assert _PRIORITY_TO_TEXT[level] in ("low", "normal", "high", "urgent")


def test_priority_label_covers_all_levels():
    assert set(_PRIORITY_LABEL.keys()) == {1, 2, 3, 4, 5}


def test_approval_label_covers_all_levels():
    assert set(_APPROVAL_LABEL.keys()) == {1, 2, 3}


# ── TaskCreate Pydantic model ─────────────────────────────────────────────────

def test_task_create_defaults():
    task = TaskCreate(title="My task")
    assert task.priority == 3
    assert task.approval_level == 2
    assert task.source == "brain"
    assert task.description == ""
    assert task.due_date is None


def test_task_create_custom_fields():
    task = TaskCreate(
        title="Ship v2",
        description="Full deploy",
        priority=5,
        approval_level=3,
        source="slack",
        tags="deploy,prod",
        assigned_to="alice",
    )
    assert task.title == "Ship v2"
    assert task.priority == 5
    assert task.approval_level == 3
    assert task.tags == "deploy,prod"
    assert task.assigned_to == "alice"


def test_task_create_requires_title():
    with pytest.raises(Exception):
        TaskCreate()  # title is required


# ── TaskUpdate Pydantic model ─────────────────────────────────────────────────

def test_task_update_all_optional():
    """TaskUpdate with no fields should be valid (all Optional)."""
    update = TaskUpdate()
    assert update.title is None
    assert update.status is None
    assert update.priority is None


def test_task_update_partial():
    update = TaskUpdate(status="in_progress", priority=4)
    assert update.status == "in_progress"
    assert update.priority == 4
    assert update.title is None
