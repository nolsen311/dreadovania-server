# Source code for this image

This image is licensed under the GNU General Public License, version 3 or (at
your option) any later version (GPL-3.0-or-later)
(`/usr/share/doc/dreadovania/LICENSE`).

- **Dreadovania Server:** https://github.com/nolsen311/dreadovania-server
  The exact commit is in the image's `org.opencontainers.image.revision` label.
  Check it with `docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' <image>`.
- **Python packages:** every package and version is listed in the repository's
  `uv.lock` for that commit, and in the SBOM attached to each release image.
  Source for each is available from https://pypi.org.
- **Debian base image:** source packages are available from https://sources.debian.org.

This image contains **no game content, keys or copyrighted game assets**.
