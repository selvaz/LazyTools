from tools.install_job_envs import in_use


def test_matches_windows_paths_case_insensitively():
    target = r"C:\ProgramData\InvestmentCommittee\envs\marketdata\Scripts\python.exe"
    assert in_use(target, [r"c:/programdata/investmentcommittee/envs/marketdata/scripts/../Scripts/PYTHON.EXE"])


def test_does_not_match_other_environments_or_prefixes():
    target = r"C:\ProgramData\InvestmentCommittee\envs\marketdata\Scripts\python.exe"
    assert not in_use(target, [r"C:\ProgramData\InvestmentCommittee\envs\marketdata2\Scripts\python.exe"])
    assert not in_use(target, [])


def test_unreadable_python_path_counts_as_in_use():
    target = r"C:\ProgramData\InvestmentCommittee\envs\marketdata\Scripts\python.exe"
    assert in_use(target, [None])
    assert in_use(target, [""])
