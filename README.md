# Dreaminko's VPM Repository

Official VRChat Package Manager (VPM) repository for Unity & VRChat packages by [Dreaminko](https://github.com/Dreaminko).

- **Landing Page**: https://vpm.dreamo.ink/
- **VPM Listing URL**: `https://vpm.dreamo.ink/index.json`
- **Repository ID**: `dev.dreaminko.vpm`

## User Installation Guide

1. **One-click add to VRChat Creator Companion (VCC)**:
   Open [`vcc://vpm/addRepo?url=https://vpm.dreamo.ink/index.json`](vcc://vpm/addRepo?url=https://vpm.dreamo.ink/index.json) or visit https://vpm.dreamo.ink/ and click **Add to VCC**.
2. **Manual add in VCC**:
   In VCC, go to **Settings → Packages → Add Repository**, paste:
   ```text
   https://vpm.dreamo.ink/index.json
   ```
   and confirm adding **Dreaminko's VPM Repository**.

> **Note**: Choose either VCC/VPM or `.unitypackage` — never install both into the same Unity project.

### Available Packages

| Package | Package ID | Repository | Description |
| :--- | :--- | :--- | :--- |
| **UniText** | `dev.dreaminko.unitext` | [Dreaminko/UniText](https://github.com/Dreaminko/UniText) | Native Unicode BMP (CJK / Multilingual) OSC shared subtitle prop and open protocol for VRChat Avatar 3.0. |

---

## Maintainer Guide: Onboarding a New Package Repository

Follow these steps to publish a new Unity / VRChat VPM package through `Dreaminko/vpm`:

1. **Conform to the GitHub Release asset contract**:
   Every non-draft GitHub Release in the upstream package repository must attach:
   - The deterministic VPM package archive: `<pkg_id>-<version>.zip`
   - A Release manifest named `package.json` with valid SemVer `version`, downloadable `url`, and 64-character hex `zipSHA256`.

2. **Register the upstream repository in `source.json`**:
   Append `<owner>/<repo>` to the `githubRepos` array in [`source.json`](source.json).

3. **Configure cross-repository dispatch (`VPM_DISPATCH_TOKEN`)**:
   Create a Fine-grained Personal Access Token scoped to `Dreaminko/vpm` with **Contents: Read and write** permission, and save it as the `VPM_DISPATCH_TOKEN` repository secret in the upstream package repository.

4. **Notify `Dreaminko/vpm` from the upstream release workflow**:
   Add the following step after publishing the GitHub Release:

   ```yaml
   - name: Notify Dreaminko VPM Repository
     env:
       VPM_DISPATCH_TOKEN: ${{ secrets.VPM_DISPATCH_TOKEN }}
     run: |
       curl -fsSL -X POST \
         -H "Accept: application/vnd.github+json" \
         -H "Authorization: Bearer ${VPM_DISPATCH_TOKEN}" \
         -H "X-GitHub-Api-Version: 2022-11-28" \
         "https://api.github.com/repos/Dreaminko/vpm/dispatches" \
         -d '{"event_type":"package-released","client_payload":{"repository":"<owner/repo>","package":"<pkg_id>","version":"<ver>","tag":"<tag>"}}'
   ```

## Local Development & Testing

```bash
# Run offline unit tests (Python 3.12 standard library only)
python scripts/test_listing.py

# Run live listing build into _site/
python scripts/build_listing.py --out-dir _site
```
