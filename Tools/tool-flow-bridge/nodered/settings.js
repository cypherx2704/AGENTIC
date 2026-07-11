/**
 * CypherX tenant Node-RED settings — hardened + white-labeled.
 *
 * Security posture (defense-in-depth; a NetworkPolicy is the primary egress control):
 *  - Admin/editor API gated by a bearer token (NODERED_ADMIN_TOKEN). The BFF injects it on
 *    every /bff/nodered/* proxied request, so the browser never holds it and direct access
 *    (bypassing the BFF) is refused.
 *  - Every HTTP-In (tool) request must carry the bridge's invoke-secret header, so only the
 *    flow-tool-bridge can trigger a workflow.
 *  - Palette install/upload disabled and Function external modules off (no ad-hoc code deps).
 *  - httpAdminRoot == the BFF path so the iframed editor's asset URLs resolve behind the proxy.
 */

const ADMIN_TOKEN = process.env.NODERED_ADMIN_TOKEN || "";
const INVOKE_SECRET = process.env.NODERED_INVOKE_SECRET || "";
const INVOKE_HEADER = (process.env.CYPHERX_INVOKE_SECRET_HEADER || "x-cypherx-tool-secret").toLowerCase();
// When true, users can install community nodes + use Function external modules — this is what
// lets them build ANY kind of tool (DB clients, API integrations, etc.). Default OFF (prod);
// the dev compose sets it ON. Egress is still bounded by the NetworkPolicy in production.
const ALLOW_PALETTE = /^(1|true|yes)$/i.test(process.env.NODERED_ALLOW_PALETTE_INSTALL || "");

function bearerOf(req) {
  const h = req.headers["authorization"] || "";
  const parts = h.split(" ");
  return parts.length === 2 && /^bearer$/i.test(parts[0]) ? parts[1] : "";
}

module.exports = {
  uiPort: parseInt(process.env.PORT || "1880", 10),
  uiHost: "0.0.0.0",
  httpAdminRoot: process.env.NODERED_ADMIN_ROOT || "/bff/nodered",
  httpNodeRoot: process.env.NODERED_HTTP_NODE_ROOT || "/flow",
  userDir: "/data",
  flowFile: "flows.json",
  credentialSecret: process.env.NODERED_CREDENTIAL_SECRET || "cypherx-dev-credential-secret",

  // ── Admin/editor token gate (all admin + editor asset routes) ────────────────
  httpAdminMiddleware: function (req, res, next) {
    if (!ADMIN_TOKEN) return next(); // dev convenience when unset
    if (bearerOf(req) === ADMIN_TOKEN) return next();
    res.status(401).json({ error: "unauthorized" });
  },

  // ── HTTP-In (tool trigger) secret gate — only the bridge may fire a workflow ──
  httpNodeMiddleware: function (req, res, next) {
    if (!INVOKE_SECRET) return next();
    if (req.headers[INVOKE_HEADER] === INVOKE_SECRET) return next();
    res.status(401).json({ error: "unauthorized" });
  },

  // ── Palette — full when ALLOW_PALETTE (build any tool), locked otherwise ──────
  externalModules: {
    autoInstall: ALLOW_PALETTE,
    palette: { allowInstall: ALLOW_PALETTE, allowUpload: ALLOW_PALETTE },
    modules: { allowInstall: ALLOW_PALETTE },
  },
  functionExternalModules: ALLOW_PALETTE,
  functionGlobalContext: {},

  // ── White-label editor ───────────────────────────────────────────────────────
  // `theme` selects a pre-built theme registered by @node-red-contrib-themes/theme-collection
  // (baked into the image's app dir — see Dockerfile). `github-dark` is the near-black + blue base
  // closest to the CypherX console; cypherx-theme.css then nudges grounds/accents to the exact
  // brand tokens. page.css is an ABSOLUTE path to a runtime-present file — it lives under /config
  // (an image layer), never /data (the named volume would shadow it).
  editorTheme: {
    theme: "github-dark",
    page: {
      title: "CypherX Tool Builder",
      favicon: undefined,
      css: "/config/cypherx-theme.css",
    },
    header: { title: "CypherX Tool Builder", url: false },
    palette: { editable: ALLOW_PALETTE },
    projects: { enabled: false },
    tours: false,
    login: { image: undefined },
  },

  logging: { console: { level: "info", metrics: false, audit: false } },
  exportGlobalContextKeys: false,
  disableEditor: false,
};
