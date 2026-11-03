# Reprise Unraid template

Import `reprise.xml` as a user template and choose the media and appdata paths. It pins a published unsigned image built from commit `2e6e9c183e886283d5539f4fc423a98bdb074605`; select a newer published immutable tag or digest to update. The media mount is read-only; no Plex database is mounted. The app listens on port 8080 and persists its token and configuration in appdata.

GPU decoding is optional and off by default (`NVIDIA_VISIBLE_DEVICES` is `void`, no device is mapped). To use a GPU, set the NVIDIA UUID, add `--runtime=nvidia --group-add=<GID>` to Extra Parameters and add the Intel `/dev/dri` nodes as Device fields; the exact settings, the checks and the fallback behavior are in the README's [Optional GPU decoding](../README.md#optional-gpu-decoding). An image published before GPU decoding was added ignores these settings and decodes on the CPU.
