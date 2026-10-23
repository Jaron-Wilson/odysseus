// The user's Odysseus theme, as a message for a mod's page (docs/mods.md,
// "Following the Odysseus theme"). Pure: modTheme.js reads the live CSS
// variables and posts what this builds. Tested under node in
// tests/test_mods_theme.py.

export const THEME_MESSAGE = 'odysseus:theme';
export const THEME_REQUEST = 'odysseus:theme-request';

// CSS variable on the Odysseus page -> variable name the mod page gets.
export const TOKEN_MAP = {
  '--bg': '--bg',
  '--fg': '--fg',
  '--panel': '--panel',
  '--border': '--border',
  '--red': '--accent',          // the theme's accent color
};

const HEX = /^#?([0-9a-f]{6})$/i;
const SAFE_VALUE = /^(#[0-9a-f]{3,8}|(rgb|hsl)a?\([0-9a-z., %\/-]{1,40}\)|[a-z]{3,20})$/i;   // a color, nothing that could carry CSS

function _lum(color) {
  const m = HEX.exec(String(color || '').trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return (0.2126 * (n >> 16 & 255) + 0.7152 * (n >> 8 & 255) + 0.0722 * (n & 255)) / 255;
}

/** 'light' or 'dark' from the background color (the same rule as
 *  static/js/uiDesign.js, which sets html[data-theme-mode]). */
export function modeOf(bg) {
  const l = _lum(bg);
  return l !== null && l > 0.55 ? 'light' : 'dark';
}

/** Readable text on the accent color: near-black or white. */
export function inkOn(color) {
  const l = _lum(color);
  return l !== null && l > 0.6 ? '#111111' : '#ffffff';
}

/** The message for a mod frame. `read(name)` gives the page's value of a CSS
 *  variable. A value that is not a plain color is dropped, so a theme can
 *  never smuggle anything but colors (and one font stack) into a mod page. */
export function buildMessage(read, font) {
  const vars = {};
  for (const [from, to] of Object.entries(TOKEN_MAP)) {
    const v = String(read(from) || '').trim();
    if (v && SAFE_VALUE.test(v)) vars[to] = v;
  }
  if (vars['--accent']) vars['--accent-ink'] = inkOn(vars['--accent']);
  const f = String(font || '').trim();
  if (f && /^[A-Za-z0-9 ,'"_-]{1,200}$/.test(f)) vars['--font'] = f;
  return { type: THEME_MESSAGE, version: 1, mode: modeOf(vars['--bg']), vars };
}
