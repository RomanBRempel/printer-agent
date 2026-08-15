# Cutting a release

A release is not finished when the tag is pushed. It is finished when a machine
on a shop floor can take the update — which is a different claim, and the two
have already come apart once.

Agents read one address, set at install time:

```
https://github.com/RomanBRempel/printer-agent/releases/latest/download/printer-agent-update.json
```

`updates.py` fetches that manifest, downloads `package_url`, and **refuses the
package unless its sha256 matches**. Everything below exists to keep that one
check honest.

## Order

**Pushing the tag publishes the release.**
[.github/workflows/release.yml](../.github/workflows/release.yml) builds the
wheel, writes the manifest from that same wheel, and creates the release with
both. Nothing is uploaded by hand.

1. Bump the version in **both** [pyproject.toml](../pyproject.toml) and
   [src/printer_agent/\_\_init\_\_.py](../src/printer_agent/__init__.py). They are
   read by different things — pip and `hello` — and a mismatch is invisible
   until the hub reports a version nobody built.
2. `python -m pytest` — green, including the live-test skip.
3. Commit, tag `vX.Y.Z`, push both. The workflow does the rest.
4. Verify from outside, the way an agent does it: fetch the `latest` manifest,
   download its `package_url`, compare the checksum, and install the result into
   a throwaway directory (`pip install --no-deps --target`).

**Do not publish a release by hand while the workflow is running.** Both write
the same two assets, and the loser's manifest ends up describing the winner's
wheel — see below. If a release has to be authored manually (the workflow is
broken, or the assets need correcting), take the checksum off the *published*
file rather than the local build:

```bash
python -m printer_agent publish-update \
  --version X.Y.Z \
  --package-url "https://github.com/RomanBRempel/printer-agent/releases/download/vX.Y.Z/printer_agent-X.Y.Z-py3-none-any.whl" \
  --sha256 from-url \
  --output dist/printer-agent-update.json
```

## Two traps, both already sprung

**The checksum has to come off the published file.** A wheel built twice from
the same commit is two different files: zip entries carry build timestamps, and
the line endings differ between a Windows checkout and the workflow's Linux
one. A manifest hashed from the local copy therefore describes a download that
nobody will ever receive — every agent fails with `downloaded package sha256
does not match manifest`, while the release looks perfectly fine from the
machine that published it.

That is exactly how 0.1.0a10 broke: a hand-run `gh release create` raced the
workflow, the workflow's Linux-built wheel replaced the uploaded one, and a
later `--clobber` swapped the wheel back while leaving the workflow's manifest
in place. The pair was inconsistent for hours, and the shop floor found it, not
the release. Let the workflow publish, and verify afterwards.

**A pre-release is invisible to the update feed.** GitHub resolves
`/releases/latest/` to the newest release *that is not a pre-release*, so
marking one hides it from every deployed agent while showing it on the releases
page. If a release is meant to be installed, it must be the latest
(`gh release edit vX.Y.Z --prerelease=false --latest`).

## The installer executable

[installer/windows/build-gui-installer-exe.ps1](../installer/windows/build-gui-installer-exe.ps1)
bundles the wheel it builds, so it installs the code it was built from rather
than falling back to the feed. It is hand-carried to a machine, not attached to
the release — releases so far carry the wheel and the manifest only. Note that
the script **rebuilds `dist/`**: run it before step 5, or the wheel you upload
will not be the one you hashed.
