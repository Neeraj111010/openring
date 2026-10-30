# Releasing OpenRing

A release publishes the Python package to [PyPI](https://pypi.org/project/openring/) and the `npx openring`
launcher to [npm](https://www.npmjs.com/package/openring). The launcher installs the PyPI release with
its own version number, so both always ship together.

## Every release

1. Set the same version in `pyproject.toml`, `openring/__init__.py` and `package.json`
   (`tests/test_release.py` fails if they differ).
2. Add the changes to `CHANGELOG.md` and commit.
3. Tag and push: `git tag v0.2.0 && git push origin v0.2.0`.

`.github/workflows/release.yml` then checks that the tag matches the versions, runs the tests, publishes
to PyPI and, once that succeeds, to npm. It uses trusted publishing: PyPI and npm trust this repository's
workflow directly, so no tokens are stored anywhere.

## One-time setup

### PyPI

Before the first release, register the workflow as a *pending publisher*
(PyPI → your account → Publishing → Add a new pending publisher → GitHub):

| Field | Value |
| --- | --- |
| PyPI project name | `openring` |
| Owner | `elifalanrocha` |
| Repository name | `openring` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

The first tagged release creates the project on PyPI.

### npm

npm only accepts a trusted publisher for a package that already exists, so the first version is
published by hand, right after the first PyPI release succeeds (the workflow skips npm with a warning
until then):

```bash
npm login
npm publish --access public
```

Then open the package on npmjs.com → Settings → Trusted publishing → GitHub Actions, and enter
owner `elifalanrocha`, repository `openring`, workflow `release.yml`, environment `npm`.
From then on, tags publish to npm automatically.
