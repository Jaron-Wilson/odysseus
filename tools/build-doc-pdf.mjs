#!/usr/bin/env node
/**
 * Render any markdown file to a paginated PDF in the jaronwilson.dev /
 * jaronwilson.org house style.
 *
 *   node tools/build-doc-pdf.mjs <input.md> <output.pdf> ["Running footer"]
 *
 * Chromium does the typesetting, which gives real pagination, page numbers
 * and control over widows and orphans that a markdown viewer cannot.
 *
 * The stylesheet below is lifted from flysdown's tools/build-paper.mjs, which
 * is where this house style was worked out. Kept as a copy rather than an
 * import so this repo renders without another project checked out next to it.
 */

import { readFile, writeFile } from 'node:fs/promises';
import { dirname, extname, resolve } from 'node:path';
import { marked } from 'marked';
import { chromium } from 'playwright';

const input = process.argv[2];
const output = process.argv[3];
if (!input || !output) {
  console.error('usage: build-doc-pdf.mjs <input.md> <output.pdf> ["Running footer"]');
  process.exit(2);
}
// Shown bottom-left on every page. Single quotes are escaped because this is
// interpolated into a CSS `content:` string.
const runningTitle = (process.argv[4] || 'jaronwilson.dev').replace(/['\\]/g, '\\$&');

const source = await readFile(input, 'utf8');
// PDF metadata title: the document's own H1, so readers and file managers see
// something better than the temp filename.
const docTitle = ((source.match(/^#\s+(.+)$/m) || [])[1] || 'Document')
  .replace(/[<>&]/g, '')
  .slice(0, 160);
let body = marked.parse(source, { gfm: true, mangle: false, headerIds: true });

/**
 * Figures. setContent has no base URL, so relative image paths in the
 * markdown would resolve to nothing; inline them as data URIs instead,
 * resolved against the markdown file's own directory. Markdown images become
 * <figure> with the alt text as the caption, which is how a paper wants them.
 */
const imagePattern = /<p><img src="([^"]+)" alt="([^"]*)"[^>]*><\/p>/g;
const inlined = [];
for (const match of body.matchAll(imagePattern)) {
  const [tag, src, alt] = match;
  if (/^(https?:|data:)/.test(src)) continue;
  const path = resolve(dirname(input), src);
  const mime = { '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.svg': 'image/svg+xml' }[extname(path).toLowerCase()] || 'application/octet-stream';
  const data = (await readFile(path)).toString('base64');
  inlined.push([tag, `<figure><img src="data:${mime};base64,${data}" alt="${alt}"><figcaption>${alt}</figcaption></figure>`]);
}
for (const [tag, figure] of inlined) body = body.replace(tag, figure);
if (inlined.length) console.log(`inlined ${inlined.length} figure(s)`);

/**
 * A table of contents from the numbered section headings, placed where the
 * markdown says <!-- toc -->. Only h2 level: the paper's sections.
 */
const slug = (text) => text.toLowerCase().replace(/<[^>]+>/g, '').replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
body = body.replace(/<h2(?![^>]*\bid=)([^>]*)>([^<]+)<\/h2>/g, (m, attrs, text) => `<h2${attrs} id="${slug(text)}">${text}</h2>`);
const headings = [...body.matchAll(/<h2[^>]*id="([^"]+)"[^>]*>([^<]+)<\/h2>/g)].map(([, id, text]) => ({ id, text }));
if (body.includes('<!-- toc -->') && headings.length) {
  const items = headings
    .filter((h) => !/^(abstract|contents)$/i.test(h.text))
    .map((h) => `<li><a href="#${h.id}">${h.text}</a></li>`)
    .join('');
  body = body.replace('<!-- toc -->', `<nav class="toc" aria-label="Contents"><h2 id="contents">Contents</h2><ol>${items}</ol></nav>`);
}

const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>${docTitle}</title>
<meta name="author" content="Jaron M. Wilson">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  /* Typeset in jaronwilson.dev and jaronwilson.org's own palette and type, so
     the paper reads as part of the same body of work. */
  :root {
    --paper: #faf8f4;
    --surface: #ffffff;
    --ink: #1a1a17;
    --muted: #6b6862;
    --border: #e8e4dc;
    --accent: #b3542b;
  }

  /* Chromium never paints the @page margin area from the root background,
     and its header/footer templates cannot paint it either (measured: the
     strips stay white). What does work is CSS margin boxes, which recent
     Chromium supports: each box carries the paper color, and the bottom ones
     carry the running title and the page counter. The side margins are zero
     and recreated as body padding, so the page box itself spans the width. */
  @page {
    size: letter;
    margin: 0.45in 0 0.4in 0;
    @top-left-corner { content: ''; background: var(--paper); }
    @top-left { content: ''; background: var(--paper); }
    @top-center { content: ''; background: var(--paper); }
    @top-right { content: ''; background: var(--paper); }
    @top-right-corner { content: ''; background: var(--paper); }
    @bottom-left-corner { content: ''; background: var(--paper); }
    @bottom-left { content: '${runningTitle}'; background: var(--paper); font: 7.5pt Inter, sans-serif; color: var(--muted); padding-left: 0.5in; padding-top: 0.06in; vertical-align: top; }
    @bottom-center { content: ''; background: var(--paper); }
    @bottom-right { content: counter(page); background: var(--paper); font: 7.5pt Inter, sans-serif; color: var(--muted); padding-right: 0.5in; padding-top: 0.06in; vertical-align: top; }
    @bottom-right-corner { content: ''; background: var(--paper); }
  }

  /* The root element's background is the page canvas in paged media, so this
     is what fills the margins too. On body alone it stopped at the text box
     and left a white frame around every page. */
  html { font-size: 8pt; background: var(--paper); -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  body {
    font-family: "Inter", -apple-system, BlinkMacSystemFont, sans-serif;
    line-height: 1.35;
    color: var(--ink);
    background: transparent;
    margin: 0;
    padding: 0 0.5in;
    hyphens: auto;
  }

  h1, h2, h3, h4 {
    font-family: "Fraunces", Georgia, serif;
    font-weight: 600;
    letter-spacing: -0.01em;
    font-optical-sizing: none;
    font-variation-settings: "opsz" 72;
    line-height: 1.35;
    color: var(--ink);
    break-after: avoid;
    page-break-after: avoid;
    hyphens: none;
  }
  h1 { font-size: 21pt; margin: 0 0 4pt; }
  h2 {
    font-size: 12pt;
    margin: 13pt 0 4pt;
    padding-bottom: 3pt;
    border-bottom: 1pt solid var(--accent);
  }
  h3 { font-size: 10pt; margin: 9pt 0 2pt; color: var(--accent); }

  /* The subtitle under the title, and the version line. */
  h1 + p strong { font-family: "Fraunces", Georgia, serif; font-weight: 400; font-size: 11.5pt; }

  p, li { orphans: 3; widows: 3; }
  p { margin: 0 0 4.5pt; }
  ul, ol { margin: 0 0 5pt; padding-left: 14pt; }
  li { margin-bottom: 1.5pt; }
  strong { font-weight: 600; }

  hr { border: 0; border-top: 1px solid var(--border); margin: 8pt 0; }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 7.6pt;
    margin: 3pt 0 7pt;
    background: var(--surface);
  }
  th, td { border: 0.5pt solid var(--border); padding: 2.5pt 4pt; text-align: left; vertical-align: top; }
  th {
    background: #f1ece3;
    font-weight: 600;
    font-size: 7pt;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--muted);
  }
  tr { break-inside: avoid; page-break-inside: avoid; }

  pre {
    background: var(--surface);
    border: 0.5pt solid var(--border);
    border-left: 2pt solid var(--accent);
    border-radius: 3pt;
    padding: 5pt 7pt;
    font-family: ui-monospace, "SFMono-Regular", Consolas, monospace;
    font-size: 6.9pt;
    line-height: 1.3;
    overflow: hidden;
    white-space: pre;
    break-inside: avoid;
    page-break-inside: avoid;
    margin: 3pt 0 7pt;
  }
  code {
    font-family: ui-monospace, "SFMono-Regular", Consolas, monospace;
    font-size: 8.3pt;
    background: #f1ece3;
    padding: 0 2pt;
    border-radius: 2pt;
  }
  pre code { background: none; padding: 0; font-size: inherit; }

  a { color: var(--accent); text-decoration: none; word-break: normal; overflow-wrap: anywhere; }

  /* The only links inside ordered lists are the reference URLs. */
  ol li a { display: block; margin-top: 1pt; }

  blockquote { margin: 0 0 6pt; padding-left: 8pt; border-left: 2pt solid var(--border); color: var(--muted); }

  figure { margin: 8pt 0 10pt; break-inside: avoid; page-break-inside: avoid; text-align: center; }
  figure img { max-width: 100%; max-height: 2.75in; width: auto; display: block; margin: 0 auto; border: 0.5pt solid var(--border); border-radius: 3pt; }
  figcaption { font-size: 7.6pt; color: var(--muted); margin-top: 4pt; line-height: 1.4; }
  figcaption strong { color: var(--ink); }

  /* Title block: authors and affiliations under the title. */
  .authors { font-size: 9.6pt; margin: 6pt 0 2pt; }
  .authors .name { font-weight: 600; }
  .affil { font-size: 8pt; color: var(--muted); margin: 0 0 8pt; }
  .keywords { font-size: 8pt; color: var(--muted); margin: 0 0 6pt; }
  .keywords strong { color: var(--ink); }

  .toc { background: var(--surface); border: 0.5pt solid var(--border); border-radius: 4pt; padding: 8pt 12pt 6pt; margin: 8pt 0 10pt; break-inside: avoid; }
  .toc h2 { border: 0; margin: 0 0 4pt; font-size: 10pt; }
  .toc ol { margin: 0; padding-left: 0; list-style: none; columns: 2; column-gap: 18pt; font-size: 8pt; }
  .toc li { margin-bottom: 1.5pt; break-inside: avoid; }
  .toc a { color: var(--ink); }
</style></head><body>${body}</body></html>`;

// --html <path> dumps what is about to be rendered, which is the quickest way
// to inspect one section without fighting a PDF viewer.
const htmlIndex = process.argv.indexOf('--html');
if (htmlIndex !== -1 && process.argv[htmlIndex + 1]) {
  await writeFile(process.argv[htmlIndex + 1], html);
  console.log(`wrote ${process.argv[htmlIndex + 1]}`);
}

const browser = await chromium.launch();
const page = await browser.newPage();
await page.setContent(html, { waitUntil: 'load' });
// Google Fonts are remote; without this the PDF renders in the fallback face.
await page.evaluate(() => document.fonts.ready);
await page.waitForTimeout(1200);

await page.pdf({
  path: output,
  format: 'Letter',
  printBackground: true,
  // The running title and page number come from the @page margin boxes in
  // the stylesheet, which is also what paints the top and bottom strips.
  displayHeaderFooter: false,
  margin: { top: '0.45in', bottom: '0.4in', left: '0', right: '0' },
});

await browser.close();

// Page count straight out of the PDF, so the target is measured not guessed.
const pdf = await readFile(output);
const pages = (pdf.toString('latin1').match(/\/Type\s*\/Page[^s]/g) || []).length;
console.log(`${output}: ${pages} pages, ${(pdf.length / 1024).toFixed(0)} KB, from ${source.split(/\s+/).length} words`);
