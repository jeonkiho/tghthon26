"""frontend/dist 를 저장소에 두지 않고 시작할 때 만드는 부분.

dist 를 커밋해 두면 프론트를 건드리는 PR 두 개가 동시에 열릴 때마다 index.html 이
충돌하고, 웹 에디터에서 한쪽 텍스트를 고르면 소스는 합쳐졌는데 번들은 한쪽만 남는다.
실제로 그렇게 CSS 가 통째로 빠진 채 머지된 적이 있다.
"""

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import frontend_build
from backend import main as main_module


def _fake_frontend(tmp_path, monkeypatch):
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "App.jsx").write_text("x", encoding="utf-8")
    (frontend / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(frontend_build, "FRONTEND", frontend)
    monkeypatch.setattr(frontend_build, "DIST", frontend / "dist")
    return frontend


def test_build_is_needed_when_there_is_no_bundle_yet(tmp_path, monkeypatch):
    _fake_frontend(tmp_path, monkeypatch)
    assert frontend_build.needs_build() is True


def test_build_is_skipped_when_the_bundle_is_newer_than_the_source(tmp_path, monkeypatch):
    frontend = _fake_frontend(tmp_path, monkeypatch)
    dist = frontend / "dist"
    dist.mkdir()
    time.sleep(0.01)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")

    # 두 번째 실행부터는 바로 떠야 한다. 매번 빌드하면 시작이 느려진다.
    assert frontend_build.needs_build() is False


def test_touching_a_source_file_makes_the_bundle_stale(tmp_path, monkeypatch):
    frontend = _fake_frontend(tmp_path, monkeypatch)
    dist = frontend / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    assert frontend_build.needs_build() is False

    time.sleep(0.01)
    (frontend / "src" / "App.jsx").write_text("changed", encoding="utf-8")
    assert frontend_build.needs_build() is True


def test_missing_frontend_explains_itself_instead_of_a_bare_404(tmp_path, monkeypatch):
    """빈 404 는 '고장났다'도 '내가 뭘 해야 한다'도 알려주지 않는다."""
    monkeypatch.setattr(main_module, "ROOT", tmp_path)   # dist 가 없는 곳을 가리킨다
    with TestClient(main_module.create_app()) as client:
        page = client.get("/")
        assert page.status_code == 503
        body = page.text
        assert "start_windows.bat" in body and "Node.js" in body
        # API 는 화면과 무관하게 살아 있어야 한다.
        assert client.get("/api/v1/health").status_code == 200
