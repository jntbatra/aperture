# Releasing the SDKs

Both packages are called `aperture-sql`. Publishing is permanent: npm lets you
unpublish only within 72 hours and PyPI never lets a version number be reused. The
workflow in `.github/workflows/release-sdk.yml` publishes only when you push a tag.

## One-time setup

**npm**
1. Create an account at npmjs.com and an *automation* access token.
2. In the GitHub repo, Settings, Secrets and variables, Actions, add it as `NPM_TOKEN`.

**PyPI** (trusted publishing, so no token is stored)
1. Create an account at pypi.org.
2. Add a *pending publisher* for project `aperture-sql`: owner `jntbatra`,
   repository `aperture`, workflow `release-sdk.yml`, environment `pypi`.
3. In the GitHub repo, Settings, Environments, create one named `pypi`.

## Releasing

1. Bump `version` in `sdk/typescript/package.json` or `sdk/python/pyproject.toml`
   (and `__version__` in `sdk/python/src/aperture_sql/__init__.py`), and commit.
2. Tag and push:
   ```bash
   git tag sdk-ts-v0.2.0 && git push origin sdk-ts-v0.2.0   # npm
   git tag sdk-py-v0.2.0 && git push origin sdk-py-v0.2.0   # PyPI
   ```

## Publishing by hand

```bash
cd sdk/typescript && npm login && npm publish --access public
cd sdk/python && python -m build && python -m twine upload dist/*
```
