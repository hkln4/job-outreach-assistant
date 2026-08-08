from types import SimpleNamespace

from app.profile_builder.github_ingest import _repo_deep_context


def _file(path, content: bytes):
    name = path.rsplit("/", 1)[-1]
    return SimpleNamespace(type="file", path=path, name=name, decoded_content=content)


def _dir(path):
    name = path.rsplit("/", 1)[-1]
    return SimpleNamespace(type="dir", path=path, name=name)


class _BareRepoNoReadme:
    """A repo with no README - the exact case CLAUDE.md section 3 calls out:
    'bare repos yield thin records - the app then samples key source files.'"""

    full_name = "someone/bare-repo"
    description = None
    stargazers_count = 0

    def get_topics(self):
        return []

    def get_languages(self):
        return {"Python": 100}

    def get_readme(self):
        raise Exception("404 no readme")

    def get_commits(self):
        return []

    def get_contents(self, path):
        if path == "":
            return [_dir("src"), _file("notes.txt", b"not code, should be skipped")]
        if path == "src":
            return [_file("src/main.py", b"def run():\n    print('hello from main')\n")]
        if path == "src/main.py":
            # PyGithub's get_contents(file_path) returns a single ContentFile,
            # not a list - re-fetching by exact path to read decoded_content
            # is the real code path this exercises.
            return _file("src/main.py", b"def run():\n    print('hello from main')\n")
        raise Exception(f"unexpected path {path}")


class _FakeGithubClient:
    def get_repo(self, full_name):
        return _BareRepoNoReadme()


def test_repo_with_no_readme_falls_back_to_sampled_source_files():
    context = _repo_deep_context(_FakeGithubClient(), "someone/bare-repo")
    assert "README: unknown (none found)" in context
    assert "No README - sampled source files instead" in context
    assert "def run():" in context
    assert "src/main.py" in context
    assert "notes.txt" not in context  # non-code file must not be sampled
