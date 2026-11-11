"""RFC-050 section 10: a red test planted on a measurement branch, to see the
ruleset block the merge into the default branch. Never merged."""


def test_planted_red() -> None:
    assert 1 + 1 == 3
