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

1. Bump the version in **both** [pyproject.toml](../pyproject.toml) and
   [src/printer_agent/\_\_init\_\_.py](../src/printer_agent/__init__.py). They are
   read by different things — pip and `hello` — and a mismatch is invisible
   until the hub reports a version nobody built.
2. `python -m pytest` — green, including the live-test skip.
3. Commit, tag `vX.Y.Z`, push both.
4. Build the wheel: `python -m build --wheel`.
5. Create the release and upload the wheel:
   `gh release create vX.Y.Z dist/printer_agent-*.whl --title vX.Y.Z --notes-file -`
6. Write the manifest **from the uploaded file**, not from the local one:

   ```bash
   python -m printer_agent publish-update \
     --version X.Y.Z \
     --package-url "https://github.com/RomanBRempel/printer-agent/releases/download/vX.Y.Z/printer_agent-X.Y.Z-py3-none-any.whl" \
     --sha256 from-url \
     --output dist/printer-agent-update.json
   ```

7. Upload the manifest: `gh release upload vX.Y.Z dist/printer-agent-update.json`
8. Verify from outside, the way an agent does it: fetch the `latest` manifest,
   download its `package_url`, compare the checksum, and install the result into
   a throwaway directory (`pip install --no-deps --target`).

## Two traps, both already sprung

**The checksum has to come off the published file.** A wheel built twice from
the same commit is two different files: zip entries carry build timestamps, and
on Windows the line endings git hands the build can differ between runs. A
manifest hashed from the local copy therefore describes a download that nobody
will ever receive — every agent fails with `downloaded package sha256 does not
match manifest`, while the release looks perfectly fine from the machine that
published it. That is what `--sha256 from-url` is for; it downloads what the
release serves and hashes that.

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
