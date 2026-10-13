"""--no-changelog keeps the release body's frame and drops the commit sections."""

import pytest


def test_no_changelog_keeps_the_frame_without_commit_sections():
    from scripts import release

    commits = [{
        "sha": "a" * 40, "short_sha": "a" * 8, "author_name": "Dev", "author_email": "dev@example.test",
        "subject": "feat: something (#5)", "category": "features", "github_author": "@dev", "coauthors": [],
    }]

    body = release.generate_changelog(commits, "rc.1-v1.2.4", "1.2.4", repo_url="https://github.com/o/r",
                                      prev_tag="v1.2.3", no_changelog=True)

    assert "Something" not in body and "@dev" not in body
    assert "<!-- HERMES_BUILDS_TABLE -->" in body
    assert "https://github.com/o/r/compare/v1.2.3...rc.1-v1.2.4" in body


def test_oversized_canary_notes_compact_before_publication(monkeypatch, capsys):
    from scripts import release

    calls = []

    def fake_generate(*_args, no_changelog=False, **_kwargs):
        calls.append(no_changelog)
        return "compact notes" if no_changelog else "x" * (release.GITHUB_RELEASE_BODY_MAX_CHARS + 1)

    monkeypatch.setattr(release, "generate_changelog", fake_generate)

    notes = release.generate_bounded_canary_changelog(
        [{}], "v1.2.3+canary.20260928T142736Z", "1.2.3",
        prev_tag=None, no_changelog=False,
    )

    assert notes == "compact notes"
    assert calls == [False, True]
    assert "using compact notes" in capsys.readouterr().out


def test_canary_notes_fail_before_publication_when_compact_frame_is_too_large(monkeypatch):
    from scripts import release

    monkeypatch.setattr(
        release, "generate_changelog",
        lambda *_args, **_kwargs: "x" * (release.GITHUB_RELEASE_BODY_MAX_CHARS + 1),
    )

    with pytest.raises(ValueError, match="Compacted canary release notes exceed"):
        release.generate_bounded_canary_changelog(
            [{}], "v1.2.3+canary.20260928T142736Z", "1.2.3",
            prev_tag=None, no_changelog=False,
        )
