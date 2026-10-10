// static/js/plansText.js
//
// The text side of the Plans editor (plans.js), kept free of the DOM so it
// can be tested on its own (tests/test_plans_text_js.py): finding the task
// lines and headings in a plan, ticking a task, and the edits the editor
// makes for Enter in a list, Tab on list items and Ctrl+B / Ctrl+I.
//
// An edit is { start, end, insert, selStart, selEnd }: replace text[start,
// end) with `insert`, then select selStart..selEnd (offsets in the new text).

// The same shapes static/js/markdown.js renders: a task is a "-" or "*" item
// starting "[ ] " or "[x] " (indented up to 12 spaces), a heading is 1 to 6
// "#" and a space. Lines inside ``` fences are neither.
const TASK_RE = /^( {0,12})[-*] \[([ xX])\] /;
const HEADING_RE = /^(#{1,6}) (.+)$/;
const FENCE_RE = /^\s*```/;
// Any list item: indent, marker ("-", "*", "+", "1." or "1)"), optional task box.
const LIST_RE = /^(\s*)([-*+]|(\d{1,9})([.)]))( +)(\[[ xX]\] +)?/;

/** Task lines and headings, in document order, with their offsets. */
export function scan(text) {
  const tasks = [];
  const headings = [];
  let inFence = false;
  let offset = 0;
  const lines = String(text ?? '').split('\n');
  lines.forEach((line, i) => {
    const at = offset;
    offset += line.length + 1;
    if (FENCE_RE.test(line)) { inFence = !inFence; return; }
    if (inFence) return;
    const t = TASK_RE.exec(line);
    if (t) {
      // The character between the brackets.
      const box = at + t[1].length + 3;
      tasks.push({ line: i, offset: at, box, done: t[2] !== ' ', text: line.slice(t[0].length).trim() });
      return;
    }
    const h = HEADING_RE.exec(line);
    if (h) headings.push({ line: i, offset: at, level: h[1].length, text: h[2].trim() });
  });
  return { tasks, headings };
}

/** Counts for the progress line. */
export function progress(text) {
  const { tasks } = scan(text);
  return { done: tasks.filter(t => t.done).length, total: tasks.length };
}

/** The edit that ticks (or unticks) task number `index`, or null. */
export function toggleTask(text, index) {
  const t = scan(text).tasks[index];
  if (!t) return null;
  const insert = t.done ? ' ' : 'x';
  return { start: t.box, end: t.box + 1, insert, selStart: null, selEnd: null };
}

function lineStart(text, pos) { return text.lastIndexOf('\n', pos - 1) + 1; }
function lineEnd(text, pos) { const i = text.indexOf('\n', pos); return i < 0 ? text.length : i; }

/**
 * Enter inside a list item: continue the list on the next line (the next
 * number for an ordered list, an empty box for a task). Enter on an item
 * with nothing after its marker ends the list instead. null = not a list
 * line, let Enter do its usual thing.
 */
export function continueList(text, selStart, selEnd = selStart) {
  const ls = lineStart(text, selStart);
  const line = text.slice(ls, lineEnd(text, selStart));
  const m = LIST_RE.exec(line);
  if (!m) return null;
  const prefixLen = m[0].length;
  if (selStart - ls < prefixLen) return null;       // caret inside the marker
  const rest = line.slice(prefixLen);
  if (!rest.trim() && selStart === selEnd) {
    // An empty item: drop its marker and stop the list.
    return { start: ls, end: ls + line.length, insert: '', selStart: ls, selEnd: ls };
  }
  const [, indent, , num, delim, gap, box] = m;
  const marker = num !== undefined ? `${parseInt(num, 10) + 1}${delim}` : m[2];
  const next = `\n${indent}${marker}${gap}${box ? '[ ] ' : ''}`;
  const caret = selStart + next.length;
  return { start: selStart, end: selEnd, insert: next, selStart: caret, selEnd: caret };
}

/**
 * Tab / Shift+Tab with the caret or selection on list items: indent each
 * line two spaces, or take up to two away. null when any selected line is
 * not a list item (Tab then moves focus as usual).
 */
export function indentList(text, selStart, selEnd, outdent = false) {
  const start = lineStart(text, selStart);
  // A selection ending at the very start of a line doesn't include that line.
  const endPos = selEnd > selStart && text[selEnd - 1] === '\n' ? selEnd - 1 : selEnd;
  const end = lineEnd(text, endPos);
  const lines = text.slice(start, end).split('\n');
  if (!lines.every(l => LIST_RE.test(l))) return null;
  let firstDelta = 0;
  let total = 0;
  const out = lines.map((l, i) => {
    let d;
    let nl;
    if (outdent) {
      const n = Math.min(2, l.length - l.trimStart().length);
      nl = l.slice(n);
      d = -n;
    } else {
      nl = '  ' + l;
      d = 2;
    }
    if (i === 0) firstDelta = d;
    total += d;
    return nl;
  });
  const insert = out.join('\n');
  if (insert === text.slice(start, end)) return null;
  const s = Math.max(start, selStart + firstDelta);
  const e = selStart === selEnd ? s : Math.max(s, selEnd + total);
  return { start, end, insert, selStart: s, selEnd: e };
}

/**
 * Ctrl+B / Ctrl+I: wrap the selection in `mark` ("**" or "*"), or unwrap it
 * when it is already wrapped. With nothing selected, insert the pair and put
 * the caret between.
 */
export function wrap(text, selStart, selEnd, mark) {
  const sel = text.slice(selStart, selEnd);
  const n = mark.length;
  // For "*", a "**" around the text is bold, not italic (unless it is "***").
  const boldOnly = (s) => n === 1 && s.startsWith('**') && !s.startsWith('***');
  // Wrapped inside the selection: "**word**" selected.
  if (sel.length >= 2 * n && sel.startsWith(mark) && sel.endsWith(mark) && !boldOnly(sel)) {
    const inner = sel.slice(n, sel.length - n);
    return { start: selStart, end: selEnd, insert: inner, selStart, selEnd: selStart + inner.length };
  }
  // Wrapped just outside the selection: "word" selected inside "**word**".
  const before = text.slice(Math.max(0, selStart - 3), selStart);
  if (sel && text.slice(selStart - n, selStart) === mark && text.slice(selEnd, selEnd + n) === mark
      && !(n === 1 && before.endsWith('**') && !before.endsWith('***'))) {
    return { start: selStart - n, end: selEnd + n, insert: sel, selStart: selStart - n, selEnd: selEnd - n };
  }
  const insert = mark + sel + mark;
  return { start: selStart, end: selEnd, insert, selStart: selStart + n, selEnd: selEnd + n };
}

/** Put `prefix` at the start of each selected line ("# ", "- ", "- [ ] "), or take it off. */
export function prefixLines(text, selStart, selEnd, prefix) {
  const start = lineStart(text, selStart);
  const end = lineEnd(text, selEnd > selStart && text[selEnd - 1] === '\n' ? selEnd - 1 : selEnd);
  const lines = text.slice(start, end).split('\n');
  const all = lines.every(l => l.startsWith(prefix));
  const out = lines.map(l => {
    if (all) return l.slice(prefix.length);
    // Swap another list marker or heading for this one rather than stacking them.
    const bare = l.replace(/^(#{1,6} |[-*+] \[[ xX]\] |[-*+] |\d{1,9}[.)] )/, '');
    return prefix + bare;
  });
  const insert = out.join('\n');
  const caret = start + insert.length;
  return { start, end, insert, selStart: lines.length === 1 ? caret : start, selEnd: caret };
}

export default { scan, progress, toggleTask, continueList, indentList, wrap, prefixLines };
