#!/bin/sh
# Install our hardened, white-labeled settings into the userDir (a mounted volume would
# otherwise shadow an image-baked copy), then start Node-RED. This guarantees our
# httpAdminRoot / httpNodeRoot / token gates / palette policy actually take effect.
set -e
mkdir -p /data
cp -f /config/settings.js /data/settings.js

# Seed a starter flow (http in -> function -> http response) ONLY on first boot, so a new tenant's
# editor opens on a pre-wired "New Tool" example instead of a blank canvas. We key off our OWN
# sentinel (.cypherx-seeded), NOT flows.json's absence: the base image ships a default flows.json
# ("Flow 1"), so an absence check would never fire. The sentinel guarantees we seed exactly once
# and NEVER overwrite a tenant's saved work on later restarts.
if [ ! -f /data/.cypherx-seeded ] && [ -f /config/tool-template.json ]; then
  cp -f /config/tool-template.json /data/flows.json
  : > /data/.cypherx-seeded
fi

cd /usr/src/node-red
exec npm --no-update-notifier --no-fund start -- --userDir /data
