# YAML Language Server in a snap #

-------------------------------------------------------------------------------

[![YAML Language Server](https://snapcraft.io/yaml-language-server/badge.svg)](https://snapcraft.io/yaml-language-server)

## Installation ##

[![Get it from the Snap Store](https://snapcraft.io/static/images/badges/en/snap-store-black.svg)](https://snapcraft.io/yaml-language-server)

``` shell
sudo snap install yaml-language-server
```

([Don't have snapd installed?](https://snapcraft.io/docs/core/install))

## Testing ##

System-level [spread](https://github.com/canonical/spread) tests in `tests/`
install the snap on Ubuntu 20.04, 22.04, 24.04 and 26.04 VMs (via
[image-garden](https://gitlab.com/zygoon/image-garden), so `/dev/kvm` access
is needed) and check that yaml-language-server validates, completes, hovers
and formats YAML against a JSON schema - both for a minimal LSP client (over
stdio and a TCP socket, with schemas from the workspace and over HTTP) and
for a real editor client (headless Neovim).

``` shell
sudo snap install image-garden

# test a locally built snap (the newest yaml-language-server_*.snap in the project dir)
snapcraft pack
spread garden:...:local

# or a store channel, e.g. latest/edge on a single release
spread garden:ubuntu-cloud-24.04:tests/...:latest_edge
```
