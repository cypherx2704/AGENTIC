# CypherX tenant Node-RED — hardened, white-labeled execution backend.
# One instance per tenant (see the flow-tool-bridge provisioner). The bridge is the ONLY
# trusted caller; a NetworkPolicy isolates each instance's egress in production.
FROM nodered/node-red:4.1.11-minimal

USER root
# Our settings live outside /data; the entrypoint copies them INTO /data on every start so a
# mounted volume can never shadow them (the official image's --settings arg is ignored).
# cypherx-theme.css is referenced by editorTheme.page.css at /config (an image layer, so it is
# always present at runtime — a /data path would be shadowed by the named volume).
COPY settings.js /config/settings.js
COPY cypherx-theme.css /config/cypherx-theme.css
COPY flows/tool-template.json /config/tool-template.json
COPY docker-entrypoint.sh /config/docker-entrypoint.sh
RUN chown -R node-red:node-red /config && chmod +x /config/docker-entrypoint.sh
USER node-red

# ── Editor theme: install into the APP dir (image layer), NOT /data ──────────────
# /data is a named volume and shadows anything baked under it at runtime; the app dir
# /usr/src/node-red is never a volume, so it survives the mount. Node-RED discovers themes
# (they are plugins) by walking up from its core-nodes dir, so /usr/src/node-red/node_modules
# is on its scan path — no /data install needed, and the locked palette does not block a
# pre-installed theme. Pinned for a reproducible hardened build. NOTE: 4.1.1 is the last line
# compatible with Node-RED 4.x (this base image is 4.1.11); 5.x requires Node-RED >=5.0.
WORKDIR /usr/src/node-red
RUN npm install --no-update-notifier --no-fund --no-audit \
      @node-red-contrib-themes/theme-collection@4.1.1

ENV PORT=1880 \
    NODERED_ADMIN_ROOT=/bff/nodered \
    NODERED_HTTP_NODE_ROOT=/flow \
    CYPHERX_INVOKE_SECRET_HEADER=x-cypherx-tool-secret

EXPOSE 1880

ENTRYPOINT ["/config/docker-entrypoint.sh"]
