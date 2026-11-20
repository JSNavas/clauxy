// Execute the actual script emitted by render_panel against browser API fakes.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const html = fs.readFileSync(0, 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function browser() {
  const events = {}, windowEvents = {}, timers = new Map(), intervals = [];
  const requests = [];
  let nextTimer = 0;
  const state = {textContent: 'live'};
  const dialog = {open: false, addEventListener() {}, showModal() {this.open = true;}};
  const commands = {};
  const pager = {dataset: {page: 'sw:1'}, disabled: false};
  const live = {dataset: {sw: '0', log: '0'}, contains: x => !!x?.inLive};
  let content = 'initial';
  Object.defineProperty(live, 'innerHTML', {
    get: () => content,
    set: value => {content = value; state.textContent = 'live'; dialog.open = false;},
  });
  const document = {
    hidden: false,
    body: {},
    activeElement: {tagName: 'BUTTON', inLive: true},
    addEventListener: (name, fn) => {events[name] = fn;},
    querySelectorAll: () => [pager],
    getElementById: id => ({live, cmds: dialog, 'open-cmds': commands, 'live-state': state})[id],
  };
  const context = {
    document,
    window: {addEventListener: (name, fn) => {windowEvents[name] = fn;}},
    AbortController,
    DOMParser: class {
      parseFromString(text) {
        return {getElementById: () => ({innerHTML: text, dataset: {sw: '0', log: '0'}})};
      }
    },
    fetch: (url, options) => new Promise((resolve, reject) => {
      requests.push({url, options, resolve, reject});
      options.signal.addEventListener('abort', () => reject(new Error('aborted')));
    }),
    setInterval: fn => {intervals.push(fn);},
    setTimeout: fn => {const id = ++nextTimer; timers.set(id, fn); return id;},
    clearTimeout: id => timers.delete(id),
  };
  vm.runInNewContext(script, context);
  return {document, events, windowEvents, timers, requests, live, state, dialog, pager,
    tick: intervals[0],
    respond: (index, content = 'updated', ok = true) => requests[index].resolve({ok, text: () => Promise.resolve(content)}),
  };
}
const flush = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  let b = browser();
  b.tick();
  assert.equal(b.requests.length, 1, 'focused button must not freeze polling');
  assert.equal(b.requests[0].options.cache, 'no-store');
  b.respond(0); await flush();
  assert.equal(b.live.innerHTML, 'updated');

  b = browser();
  b.document.activeElement = {tagName: 'TEXTAREA', inLive: true};
  b.tick(); assert.equal(b.requests.length, 0, 'an editable field must be preserved');
  b.document.activeElement = b.document.body;
  b.tick(); assert.equal(b.requests.length, 1);

  b = browser();
  b.tick(); b.requests[0].reject(new Error('service restarting')); await flush();
  assert.equal(b.state.textContent, 'Reconectando…');
  b.tick(); b.respond(1, 'after restart'); await flush();
  assert.equal(b.live.innerHTML, 'after restart', 'retry must resume without a page reload');
  assert.equal(b.state.textContent, 'live');

  b = browser();
  b.tick(); b.respond(0, '', false); await flush();
  assert.equal(b.state.textContent, 'Reconectando…', 'HTTP failures also need recovery');
  b.tick(); b.respond(1); await flush();
  assert.equal(b.live.innerHTML, 'updated');

  b = browser();
  b.tick(); b.tick();
  assert.equal(b.requests.length, 1, 'polls must not overlap');
  [...b.timers.values()][0](); await flush();
  assert.equal(b.requests[0].options.signal.aborted, true, 'stuck request must time out');
  b.tick(); assert.equal(b.requests.length, 2, 'timeout must release the in-flight lock');

  b = browser();
  b.tick(); b.pager.onclick();
  b.respond(0, 'outdated'); await flush();
  assert.equal(b.live.innerHTML, 'initial', 'old poll must not undo a queued pager action');
  assert.equal(b.requests.length, 2);
  assert.match(b.requests[1].url, /sw=1/);
  b.respond(1); await flush();
  assert.equal(b.live.innerHTML, 'updated');

  b = browser();
  b.document.hidden = true; b.tick();
  assert.equal(b.requests.length, 0);
  b.document.hidden = false; b.events.visibilitychange();
  assert.equal(b.requests.length, 1, 'returning to the tab must poll immediately');

  b = browser();
  b.events.pointerdown(); b.tick(); assert.equal(b.requests.length, 0);
  b.events.pointerup(); b.tick(); assert.equal(b.requests.length, 1);
  b.respond(0); await flush();
  b.events.pointerdown(); b.windowEvents.blur(); b.windowEvents.focus();
  assert.equal(b.requests.length, 2, 'tab changes must not leave pointer state stuck');

  b = browser();
  b.dialog.open = true; b.tick(); b.respond(0); await flush();
  assert.equal(b.dialog.open, true, 'an open Commands dialog must remain open');
  console.log('PASS: live panel polling, focused buttons, editable fields, restart recovery, HTTP errors, timeout, paging, visibility, pointer state, and dialog preservation');
})().catch(error => {console.error(error); process.exitCode = 1;});
