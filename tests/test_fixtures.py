from fixtures import git, patch_id, start_conflict


def test_branches_exist(scenario):
    names = git(scenario.repo, "branch", "--format=%(refname:short)").stdout.split()
    assert {"prod", "uat", "dev"} <= set(names)


def test_backport_has_cherry_pick_trailer(scenario):
    msg = git(scenario.repo, "log", "-1", "--format=%B", scenario.backport).stdout
    assert f"(cherry picked from commit {scenario.hotfix})" in msg


def test_backport_patch_id_differs_from_original(scenario):
    assert patch_id(scenario.repo, scenario.hotfix) != patch_id(
        scenario.repo, scenario.backport
    )


def test_promoting_dev_into_uat_conflicts(scenario):
    result = start_conflict(scenario)
    assert result.returncode != 0
    assert "settings.py" in git(scenario.repo, "ls-files", "-u").stdout
