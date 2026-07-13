/*
 * CypherX Tool Builder — client-side autosave + recovery for the Node-RED editor.
 *
 * Injected into the editor page via `editorTheme.page.scripts` (settings.js). It runs INSIDE the
 * Node-RED editor iframe, where the `RED` global gives access to the current (undeployed) flow.
 *
 * Why this exists: undeployed edits live ONLY in the editor's browser memory. A session expiry, an
 * accidental tab reload, or the console's "Reload editor" button unloads the iframe and the work is
 * gone — Node-RED only persists to flows.json on an explicit Deploy. This snapshots the in-editor
 * flow to localStorage (periodically, on change, and on unload) and offers to recover it on the next
 * load, so unsaved work survives any unload. A successful Deploy clears the draft (the work is now
 * safely persisted server-side).
 *
 * Design rule: this is a progressive enhancement and MUST NEVER break the editor. Every touch of the
 * RED API is feature-detected and wrapped in try/catch; on anything unexpected it silently no-ops.
 *
 * Note: localStorage is per-origin and the editor is same-origin behind the BFF, so the draft written
 * by one editor load is readable by the next. It is NOT synced across devices (that would need a
 * server-side draft store); it covers the reload / expiry / accidental-close loss vectors.
 */
(function () {
  'use strict';

  var KEY = 'cypherx:toolbuilder:autosave:v1';
  var SAVE_DEBOUNCE_MS = 1500; // coalesce rapid edits
  var SAVE_INTERVAL_MS = 15000; // periodic safety net
  var MAX_BYTES = 4 * 1024 * 1024; // don't blow the localStorage quota on a huge flow

  function redReady() {
    return (
      typeof window.RED !== 'undefined' &&
      window.RED &&
      window.RED.nodes &&
      window.RED.events &&
      typeof window.RED.nodes.createCompleteNodeSet === 'function'
    );
  }

  function isDirty() {
    try {
      return typeof RED.nodes.dirty === 'function' ? RED.nodes.dirty() : true;
    } catch (e) {
      return true; // if we can't tell, assume there may be unsaved work
    }
  }

  function currentFlowJson() {
    try {
      return JSON.stringify(RED.nodes.createCompleteNodeSet());
    } catch (e) {
      return null;
    }
  }

  function saveDraft(reason) {
    try {
      if (!redReady() || !isDirty()) return; // nothing unsaved → nothing to protect
      var flows = currentFlowJson();
      if (!flows || flows === '[]' || flows.length > MAX_BYTES) return;
      window.localStorage.setItem(
        KEY,
        JSON.stringify({ savedAt: Date.now(), reason: reason || 'auto', flows: flows }),
      );
    } catch (e) {
      /* quota / serialization / storage disabled — ignore */
    }
  }

  function readDraft() {
    try {
      var raw = window.localStorage.getItem(KEY);
      if (!raw) return null;
      var rec = JSON.parse(raw);
      return rec && typeof rec.flows === 'string' && rec.flows !== '[]' ? rec : null;
    } catch (e) {
      return null;
    }
  }

  function clearDraft() {
    try {
      window.localStorage.removeItem(KEY);
    } catch (e) {
      /* ignore */
    }
  }

  // ── capture: periodic + change-driven + on unload ─────────────────────────────
  var debounceTimer = null;
  function scheduleSave() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(function () {
      saveDraft('change');
    }, SAVE_DEBOUNCE_MS);
  }

  function wireCapture() {
    ['nodes:change', 'flows:change', 'nodes:add', 'nodes:remove', 'workspace:change'].forEach(
      function (ev) {
        try {
          RED.events.on(ev, scheduleSave);
        } catch (e) {
          /* event not present in this version — fine */
        }
      },
    );

    setInterval(function () {
      saveDraft('interval');
    }, SAVE_INTERVAL_MS);

    // Best-effort snapshot when the iframe/tab is torn down (covers reload, "Reload editor",
    // navigation to /login on session expiry). pagehide is the most reliable across browsers.
    window.addEventListener('pagehide', function () {
      saveDraft('pagehide');
    });
    window.addEventListener('beforeunload', function () {
      saveDraft('beforeunload');
    });
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden') saveDraft('hidden');
    });

    // A successful Deploy persists the flow server-side → the draft is no longer needed.
    try {
      RED.events.on('deploy', clearDraft);
    } catch (e) {
      /* ignore */
    }
  }

  // ── recovery: offer to restore a draft on load ────────────────────────────────
  function importDraft(rec) {
    var opts = { generateIds: true, addFlow: true }; // fresh copies → never collide with loaded flows
    try {
      var parsed = JSON.parse(rec.flows);
      if (RED.view && typeof RED.view.importNodes === 'function') {
        RED.view.importNodes(parsed, opts);
        return true;
      }
      if (RED.clipboard && typeof RED.clipboard.importNodes === 'function') {
        RED.clipboard.importNodes(rec.flows, opts);
        return true;
      }
    } catch (e) {
      // Fall back to the string form (older signatures accepted a JSON string).
      try {
        if (RED.view && typeof RED.view.importNodes === 'function') {
          RED.view.importNodes(rec.flows, opts);
          return true;
        }
      } catch (e2) {
        /* give up — the draft is kept so nothing is lost */
      }
    }
    return false;
  }

  var offered = false;
  function offerRestore() {
    if (offered) return;
    var rec = readDraft();
    if (!rec) return;
    offered = true;
    try {
      var when = new Date(rec.savedAt).toLocaleString();
      var note = RED.notify(
        '<p><b>Recover unsaved Tool Builder changes?</b></p>' +
          '<p>We found workflow edits from ' +
          when +
          ' that were never deployed. Restore them into the editor?</p>',
        {
          modal: true,
          fixed: true,
          type: 'warning',
          buttons: [
            {
              text: 'Discard',
              click: function () {
                clearDraft();
                note.close();
              },
            },
            {
              text: 'Restore',
              class: 'primary',
              click: function () {
                var ok = importDraft(rec);
                note.close();
                // Keep the draft until the user Deploys — so an imperfect import never loses data.
                try {
                  RED.notify(
                    ok
                      ? 'Restored your unsaved changes. Review them, then Deploy to save.'
                      : "Couldn't auto-restore — your draft is preserved.",
                    { type: ok ? 'success' : 'error', timeout: 7000 },
                  );
                } catch (e) {
                  /* ignore */
                }
              },
            },
          ],
        },
      );
    } catch (e) {
      offered = false; // notify failed — allow a later retry
    }
  }

  // ── boot: wait until RED is initialised, then wire capture + schedule recovery ─
  function boot() {
    if (!redReady()) return false;
    wireCapture();

    var didOffer = false;
    function tryOffer() {
      if (didOffer) return;
      didOffer = true;
      // Let the editor finish importing the deployed flows first, then offer recovery.
      setTimeout(offerRestore, 900);
    }
    try {
      RED.events.on('flows:loaded', tryOffer); // fires after the initial flow load (4.x)
    } catch (e) {
      /* ignore */
    }
    setTimeout(tryOffer, 3000); // fallback if the event name differs in this version
    return true;
  }

  var tries = 0;
  var poll = setInterval(function () {
    tries += 1;
    if (boot() || tries > 120) clearInterval(poll); // poll up to ~60s for RED to appear
  }, 500);
})();
