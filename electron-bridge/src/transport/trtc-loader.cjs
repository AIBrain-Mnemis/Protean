/**
 * Lazy CJS loader for `trtc-electron-sdk`.
 *
 * The SDK is shipped as a native (.node) addon, marked as an
 * optionalDependency so the development path doesn't require
 * its native binary to install. This shim returns `null` instead of
 * throwing when the package isn't available, letting transport/trtc.ts
 * decide what to do (typically: don't advertise transport.trtc capability).
 *
 * Two reasons this is .cjs not .ts/.cts:
 *   1) The SDK uses CommonJS internally and patches process.dlopen paths
 *      at require() time; doing this from ESM via dynamic import works on
 *      most platforms but is finicky.
 *   2) Keeps the lazy-load failure-mode in pure JS so a missing native
 *      build never makes our TypeScript pipeline angry.
 */

let cachedSdk = null;
let triedLoad = false;
let loadError = null;

function loadTrtc() {
  if (triedLoad) return cachedSdk;
  triedLoad = true;
  try {
    // The SDK exports the TRTCCloud class as `default` because it uses
    // `module.exports.default = TRTCCloud` (verified in trtc.io docs).
    cachedSdk = require('trtc-electron-sdk');
  } catch (err) {
    loadError = err && err.message ? err.message : String(err);
    cachedSdk = null;
  }
  return cachedSdk;
}

function loadTrtcDefine() {
  if (!loadTrtc()) return null;
  try {
    return require('trtc-electron-sdk/liteav/trtc_define');
  } catch (err) {
    return null;
  }
}

module.exports = {
  /**
   * Returns `{TRTCCloud, define}` if the SDK is loadable, or
   * `{TRTCCloud: null, define: null, error: <message>}` otherwise.
   *
   * `TRTCCloud` is the class (or namespace with default export depending
   * on SDK version); callers should handle both shapes.
   */
  loadTrtcSdk() {
    const sdk = loadTrtc();
    if (!sdk) {
      return { TRTCCloud: null, define: null, error: loadError };
    }
    const trtcCloud = sdk.default || sdk;
    return { TRTCCloud: trtcCloud, define: loadTrtcDefine(), error: null };
  },
};
