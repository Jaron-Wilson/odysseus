// A stand-in for a module that loads on demand.
//
// `lazyModule(loader, opts)` returns an object with the same method names as
// the real module. Before the real module has loaded:
//   - names in `sync` answer with a fixed value (a closed panel is not open),
//   - names in `noop` do nothing (closing something that never opened),
//   - every other method loads the module, then calls the real method, and
//     returns a promise of its result.
// Once loaded, every access goes straight to the real module. `onLoad(real)`
// runs once, before any queued call, so init() always comes first.
// `.load()` starts the load (used to prefetch at idle); `.loaded` says if done.
export function lazyModule(loader, opts = {}) {
  const sync = opts.sync || {};
  const noop = new Set(opts.noop || []);
  let real = null;
  let pending = null;
  const load = () => {
    if (!pending) {
      pending = Promise.resolve(loader()).then((m) => {
        real = (m && m.default) || m;
        if (opts.onLoad) opts.onLoad(real);
        return real;
      }).catch((e) => { pending = null; throw e; });
    }
    return pending;
  };
  const own = { load, get loaded() { return !!real; }, get real() { return real; } };
  return new Proxy(own, {
    get(_t, prop) {
      if (prop in own) return own[prop];
      if (typeof prop === 'symbol' || prop === 'then') return undefined;
      if (real) {
        const v = real[prop];
        return typeof v === 'function' ? v.bind(real) : v;
      }
      if (prop in sync) return () => sync[prop];
      if (noop.has(prop)) return () => undefined;
      return (...args) => load().then((m) => m[prop](...args));
    },
  });
}

// Run `fn` once the first screen is up and the browser has a quiet moment.
export function whenIdle(fn, timeout = 1500) {
  const go = () => (window.requestIdleCallback
    ? window.requestIdleCallback(fn, { timeout })
    : setTimeout(fn, 200));
  if (document.readyState === 'complete') go();
  else window.addEventListener('load', go, { once: true });
}
