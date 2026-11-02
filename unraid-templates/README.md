# Reprise Unraid template

Import `reprise.xml` as a user template. Before starting it, select an existing immutable `ghcr.io/nphil/reprise:sha-<full-commit>` tag or digest, and choose the media and appdata paths. The media mount is read-only; no Plex database is mounted. The app listens on port 8080 and persists its token and configuration in appdata. The image must first be built and published; this template does not create it.
