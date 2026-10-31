import pytest

from fixtures import patch_id as fixture_patch_id
from trustmerge.git import (
    GitError,
    is_ancestor,
    list_branches,
    patch_id,
    rev_parse,
    run_git,
)


def test_run_git_returns_stdout(scenario):
    assert run_git(scenario.repo, "rev-parse", "HEAD") == scenario.hotfix


def test_run_git_raises_on_failure(scenario):
    with pytest.raises(GitError):
        run_git(scenario.repo, "rev-parse", "no-such-ref")


def test_run_git_check_false_does_not_raise(scenario):
    run_git(scenario.repo, "rev-parse", "no-such-ref", check=False)


def test_list_branches(scenario):
    assert set(list_branches(scenario.repo)) == {"prod", "uat", "dev"}


def test_rev_parse_resolves_branch(scenario):
    assert rev_parse(scenario.repo, "prod") == scenario.hotfix


def test_is_ancestor_true(scenario):
    assert is_ancestor(scenario.repo, scenario.initial, "prod") is True


def test_is_ancestor_false(scenario):
    assert is_ancestor(scenario.repo, scenario.dev_change, "prod") is False


def test_patch_id_matches_reference(scenario):
    assert patch_id(scenario.repo, scenario.hotfix) == fixture_patch_id(
        scenario.repo, scenario.hotfix
    )


def test_patch_id_differs_for_edited_backport(scenario):
    assert patch_id(scenario.repo, scenario.hotfix) != patch_id(
        scenario.repo, scenario.backport
    )
