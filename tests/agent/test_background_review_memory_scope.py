"""The review fork's memory access must follow the trigger that fired (#105921).

``spawn_background_review_thread`` already received ``review_memory`` /
``review_skills`` but used them only to pick the prompt — the tool whitelist
granted the whole ``memory`` toolset whenever the profile had memory enabled,
so a skill-nudge fork held ``remove``/``replace`` on MEMORY.md it was never
asked to use. These tests pin the scope-aware whitelist and the pass-through
from ``spawn_background_review_thread`` down to it.
"""

from __future__ import annotations

import os
import sys
import pytest
from types import SimpleNamespace
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import agent.background_review as bg  # noqa: E402


def _review_agent(memory_enabled=True, user_profile_enabled=False) -> SimpleNamespace:
    """The whitelist only reads the profile's memory flags off the fork."""
    return SimpleNamespace(_memory_enabled=memory_enabled, _user_profile_enabled=user_profile_enabled)


class TestReviewToolWhitelistScope:
    def test_skill_only_review_omits_memory_tool(self):
        whitelist, _extra = bg._review_tool_whitelist(_review_agent(), None, review_memory=False)
        assert "memory" not in whitelist
        assert "skill_manage" in whitelist  # the skill review keeps its own surface

    def test_memory_review_keeps_memory_tool(self):
        whitelist, _extra = bg._review_tool_whitelist(_review_agent(), None, review_memory=True)
        assert "memory" in whitelist

    def test_memory_disabled_profile_stays_memory_free(self):
        whitelist, _extra = bg._review_tool_whitelist(
            _review_agent(memory_enabled=False, user_profile_enabled=False), None, review_memory=True)
        assert "memory" not in whitelist

    def test_default_scope_is_memoryless_fail_closed(self):
        # Unknown trigger (default) must not grant memory: the incident fork was a
        # skill review that used memory nobody asked it to touch.
        whitelist, _extra = bg._review_tool_whitelist(_review_agent(), None)
        assert "memory" not in whitelist


class TestSpawnForwardsScope:
    def test_target_passes_review_memory_to_worker(self):
        captured = {}

        def fake_worker(agent, messages_snapshot, prompt, task_cfg=None, review_run=None,
                        review_memory=False, explicit=False):
            captured["review_memory"] = review_memory

        agent = SimpleNamespace()
        with patch.object(bg, "_run_review_in_thread", fake_worker):
            target, _prompt = bg.spawn_background_review_thread(
                agent, [], review_memory=False, review_skills=True)
            target()
            assert captured["review_memory"] is False

            target, _prompt = bg.spawn_background_review_thread(
                agent, [], review_memory=True, review_skills=False)
            target()
            assert captured["review_memory"] is True


class TestExplicitRefineOrigin:
    """``/refine`` (explicit) must not inherit the unattended-review origin: the user asked
    for that review, so its fork keeps the full memory operation set and the delete gate
    does not apply (#105921 review follow-up)."""

    def test_target_passes_explicit_to_worker(self):
        captured = {}

        def fake_worker(agent, messages_snapshot, prompt, task_cfg=None, review_run=None,
                        review_memory=False, explicit=False):
            captured["explicit"] = explicit

        with patch.object(bg, "_run_review_in_thread", fake_worker):
            target, _prompt = bg.spawn_background_review_thread(
                SimpleNamespace(), [], review_memory=True, explicit=True)
            target()
            assert captured["explicit"] is True

    @pytest.mark.parametrize("explicit", [True, False])
    def test_fork_keeps_background_review_origin_and_carries_attendedness(self, explicit):
        """Every curator/skill guard keys on the background_review origin, so an explicit /refine
        must NOT change the origin; attendedness rides on the fork as its own flag."""
        forks = []

        def fake_build(agent, task_cfg=None, *, max_iterations, write_origin="background_review"):
            fork = SimpleNamespace(
                _memory_enabled=True, _user_profile_enabled=False, _memory_write_origin=write_origin,
                run_conversation=lambda **kw: None, _session_messages=[])
            forks.append(fork)
            return fork, {}, False

        noop = lambda *a, **k: None
        with patch.object(bg, "build_cache_parity_fork", fake_build), \
                patch.object(bg, "_track_review_fork", noop), \
                patch.object(bg, "_snapshot_review_usage", lambda a: {}), \
                patch.object(bg, "_record_review_usage_to_parent", noop), \
                patch.object(bg, "finish_background_review_run", noop), \
                patch.object(bg, "_release_fork_clients", noop):
            bg._run_review_fork(SimpleNamespace(), [], "p", None, None, bg._ReviewForkState(), True, explicit)
        (fork,) = forks
        assert fork._memory_write_origin == "background_review"
        assert fork._review_attended is explicit


class TestConsolidationProposalSurfaces:
    """When write approval is enabled, staged consolidation must surface through
    ``summarize_background_review_actions``; with approval off, consolidation applies directly."""

    def _store(self, tmp_path, monkeypatch):
        from tools.memory_tool_store import MemoryStore

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
        store = MemoryStore(memory_char_limit=500, user_char_limit=300)
        store.load_from_disk()
        return store

    def test_staged_proposal_surfaces_in_summary(self, tmp_path, monkeypatch):
        import json

        from tools.memory_tool import memory_tool
        from tools.skill_provenance import set_current_write_origin, reset_current_write_origin

        store = self._store(tmp_path, monkeypatch)
        monkeypatch.setattr("tools.write_approval.write_approval_enabled", lambda subsystem: True)
        assert store.add("memory", "standing rule entry")["success"] is True

        token = set_current_write_origin("background_review")
        try:
            raw = memory_tool(
                action="replace", old_text="standing rule", content="consolidated entry", store=store)
        finally:
            reset_current_write_origin(token)
        result = json.loads(raw)
        assert result["staged"] is True and result["proposal_staged"] is True

        review_messages = [
            {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "memory", "arguments": json.dumps(
                {"action": "replace", "old_text": "standing rule", "content": "consolidated entry"})}}]},
            {"role": "tool", "tool_call_id": "c1", "content": raw},
        ]
        actions = bg.summarize_background_review_actions(review_messages, [])
        assert any("staged for your approval" in a for a in actions)

    def test_near_limit_consolidation_applies_when_approval_off(self, tmp_path, monkeypatch):
        """With approval off, a review may follow the near-limit hint and consolidate
        automatically, preserving the workflow configured by the user."""
        import json

        from tools.memory_tool import memory_tool
        from tools.skill_provenance import set_current_write_origin, reset_current_write_origin

        store = self._store(tmp_path, monkeypatch)
        monkeypatch.setattr("tools.write_approval.write_approval_enabled", lambda subsystem: False)
        assert store.add("memory", "seed entry one")["success"] is True
        assert store.add("memory", "x" * 600)["success"] is False

        token = set_current_write_origin("background_review")
        try:
            add_raw = memory_tool(action="add", content="y" * 600, store=store)
            replace_raw = memory_tool(
                action="replace", old_text="seed entry one", content="merged entry", store=store)
        finally:
            reset_current_write_origin(token)
        assert json.loads(add_raw)["success"] is False
        replace_result = json.loads(replace_raw)
        assert replace_result["success"] is True
        assert replace_result.get("staged") is not True
        assert "seed entry one" not in store._entries_for("memory")
        assert "merged entry" in store._entries_for("memory")
