import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import save_state  # noqa: E402


def sh(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def write(d, name, data):
    (d / name).write_text(json.dumps(data, indent=1) + "\n")


def test_merge_per_agent_keeps_both_runs_and_the_priority_wins_a_clash():
    base = {"agents": {"a": {"cash": 1, "positions": {}}, "b": {"cash": 1, "positions": {}}}, "x": 1}
    ours = {"agents": {"a": {"cash": 2, "positions": {}}, "b": {"cash": 1, "positions": {}}}, "x": 1}
    theirs = {"agents": {"a": {"cash": 1, "positions": {}}, "b": {"cash": 5, "positions": {"S": 1}}, "c": {"cash": 9}}, "x": 2}
    m = save_state.merge3(base, ours, theirs, ours_first=True)
    assert m["agents"]["a"]["cash"] == 2 and m["agents"]["b"]["cash"] == 5 and "c" in m["agents"] and m["x"] == 2
    clash = {"agents": {"a": {"cash": 3, "positions": {}}, "b": base["agents"]["b"]}, "x": 1}
    assert save_state.merge3(base, ours, clash, ours_first=True)["agents"]["a"]["cash"] == 2
    assert save_state.merge3(base, ours, clash, ours_first=False)["agents"]["a"]["cash"] == 3
    cfg_b = {"agents": [{"id": "a", "v": 1}, {"id": "b", "v": 1}]}
    cfg_o = {"agents": [{"id": "a", "v": 2}, {"id": "b", "v": 1}]}
    cfg_t = {"agents": [{"id": "a", "v": 1}, {"id": "b", "v": 1}, {"id": "n", "v": 0}]}
    assert save_state.merge3(cfg_b, cfg_o, cfg_t, True)["agents"] == [{"id": "a", "v": 2}, {"id": "b", "v": 1}, {"id": "n", "v": 0}]


def test_two_runs_at_once_both_land_without_mixing_an_agent(tmp_path, monkeypatch):
    remote, one, two = tmp_path / "remote.git", tmp_path / "one", tmp_path / "two"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(one)], check=True, capture_output=True)
    for d in (one,):
        sh(d, "config", "user.email", "t@t"), sh(d, "config", "user.name", "t")
    state = {"agents": {"a": {"cash": 100, "positions": {}}, "b": {"cash": 100, "positions": {}}}}
    write(one, "state.json", state)
    write(one, "config.json", {"agents": [{"id": "a"}, {"id": "b"}]})
    sh(one, "add", "-A"), sh(one, "commit", "-qm", "init"), sh(one, "push", "-q", "origin", "HEAD:main")
    subprocess.run(["git", "clone", "-q", str(remote), str(two)], check=True, capture_output=True)
    sh(two, "config", "user.email", "t@t"), sh(two, "config", "user.name", "t")
    # run one: agent a buys; run two (started from the same state): agent b buys, a's cash line also moves
    write(one, "state.json", {"agents": {"a": {"cash": 40, "positions": {"X": 1}}, "b": state["agents"]["b"]}})
    write(two, "state.json", {"agents": {"a": {"cash": 100, "positions": {}}, "b": {"cash": 10, "positions": {"Y": 3}}}})
    monkeypatch.setattr(save_state, "AGENTS_DIR", one)
    assert save_state.save("one", True, ["."]) == 0
    monkeypatch.setattr(save_state, "AGENTS_DIR", two)
    assert save_state.save("two", False, ["."]) == 0
    final = tmp_path / "final"
    subprocess.run(["git", "clone", "-q", str(remote), str(final)], check=True, capture_output=True)
    got = json.loads((final / "state.json").read_text())["agents"]
    assert got["a"] == {"cash": 40, "positions": {"X": 1}} and got["b"] == {"cash": 10, "positions": {"Y": 3}}


def test_a_broken_state_is_never_pushed(tmp_path, monkeypatch):
    d = tmp_path / "d"
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    sh(d, "config", "user.email", "t@t"), sh(d, "config", "user.name", "t")
    write(d, "state.json", {"agents": {}})
    sh(d, "add", "-A"), sh(d, "commit", "-qm", "init")
    (d / "state.json").write_text("{not json")
    monkeypatch.setattr(save_state, "AGENTS_DIR", d)
    assert save_state.save("x", True, ["."]) == 1
