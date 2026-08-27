import { useMemo } from "react";

/**
 * Layer 2 of artifact isolation.
 *
 * The server has already run the HTML through an allowlist. This frame assumes
 * that failed, and contains the blast radius anyway:
 *
 * - `sandbox` with **no** `allow-same-origin` puts the document in a unique
 *   opaque origin. It cannot read cookies, `localStorage`, or the parent DOM,
 *   even though it is served from the same site.
 * - `sandbox` without `allow-scripts` means nothing executes at all.
 * - A `default-src 'none'` CSP inside the document blocks every network
 *   request, so there is no channel to exfiltrate over even if code did run.
 * - `srcdoc` rather than a URL keeps the content out of the browser's history,
 *   the referrer chain, and any URL-based cache.
 *
 * Turning scripts on adds `allow-scripts` — and never `allow-same-origin`.
 * Granting both together is the one combination that defeats the sandbox
 * entirely, because a script can then reach out and remove its own sandbox
 * attribute from the parent document.
 */

const CSP = [
  "default-src 'none'",
  "style-src 'unsafe-inline'",     // inline <style> is how artifacts lay out
  "img-src data:",                 // inline images only; no remote fetches
  "font-src 'none'",
  "connect-src 'none'",
  "form-action 'none'",
  "base-uri 'none'",
].join("; ");

export function ArtifactFrame({
  html,
  allowScripts,
  title,
}: {
  html: string;
  allowScripts: boolean;
  title: string;
}) {
  const srcDoc = useMemo(() => {
    const scriptPolicy = allowScripts ? "script-src 'unsafe-inline'" : "script-src 'none'";
    return `<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="${CSP}; ${scriptPolicy}">
<style>
  :root { color-scheme: light dark; }
  html, body { margin: 0; }
  body {
    padding: 20px;
    font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    line-height: 1.6;
    background: #fff;
    color: #1c1a18;
  }
  @media (prefers-color-scheme: dark) {
    body { background: #1e1d18; color: #f0eee9; }
    a { color: #e08b5f; }
  }
  img, table { max-width: 100%; }
  /* Wide content scrolls inside itself rather than breaking the layout. */
  table { display: block; overflow-x: auto; border-collapse: collapse; }
  pre { overflow-x: auto; }
</style>
</head><body>${html}</body></html>`;
  }, [html, allowScripts]);

  return (
    <iframe
      title={title}
      srcDoc={srcDoc}
      sandbox={allowScripts ? "allow-scripts" : undefined}
      className="h-full w-full border-0 bg-white dark:bg-[#1e1d18]"
      referrerPolicy="no-referrer"
    />
  );
}
