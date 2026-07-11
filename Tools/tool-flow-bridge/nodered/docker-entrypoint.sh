#!/bin/sh
# Install our hardened, white-labeled settings into the userDir (a mounted volume would
# otherwise shadow an image-baked copy), then start Node-RED. This guarantees our
# httpAdminRoot / httpNodeRoot / token gates / palette policy actually take effect.
set -e
mkdir -p /data
cp -f /config/settings.js /data/settings.js
cd /usr/src/node-red
exec npm --no-update-notifier --no-fund start -- --userDir /data
