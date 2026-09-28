// Verify the actual UI handlers against a small DOM, including saved settings.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const elements = new Map();
function element(value = '') {
  const classes = new Set(), listeners = {}, attributes = {};
  return {value, listeners, attributes, style: {setProperty(){}}, dataset: {},
    classList: {toggle(name, selected) {selected ? classes.add(name) : classes.delete(name);},
      contains(name) {return classes.has(name);}},
    clientWidth: 0, clientHeight: 0, append(){}, appendChild(){}, replaceChildren(){},
    addEventListener(name, handler) {listeners[name] = handler;}, removeEventListener(){},
    setAttribute(name, value) {attributes[name] = value;}};
}
for (const match of html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)) {
  const value = match[0].match(/\bvalue="([^"]*)"/);
  const threshold = match[0].match(/\bdata-mono-threshold="([^"]*)"/);
  const item = element(value ? value[1] : '');
  if (threshold) item.dataset.monoThreshold = threshold[1];
  elements.set(match[1], item);
}
const presets = [...elements.values()].filter(item => item.dataset.monoThreshold);
assert.equal(presets.length, 3);
const storage = new Map(), timers = [];
const document = {getElementById(id) {
  assert(elements.has(id), `Missing HTML element ${id}`); return elements.get(id);
}, querySelectorAll(selector) {return selector === '[data-mono-threshold]' ? presets : [];},
  addEventListener(){}, createElement(){return element();}};
const context = vm.createContext({document, window: {addEventListener(){}},
  localStorage: {getItem(key) {return storage.get(key) ?? null;},
    setItem(key, value) {storage.set(key, value);}},
  setTimeout(handler, delay) {timers.push({handler, delay}); return timers.length;}, clearTimeout(){},
  requestAnimationFrame(){}, console});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8') + `
this.presetTests={restore,configureJob(){generated=true;plotReady=true;dirty=false;
  plotPreview='data:image/png;base64,test';previewMode='plot';renderControls();},
  snapshot(){return {dirty,previewMode,paletteMode,threshold:$('mono_threshold').value};}};`, context);
const controller = context.presetTests;
const expectSelected = threshold => {
  for (const preset of presets) {
    const selected = Number(preset.dataset.monoThreshold) === threshold;
    assert.equal(preset.classList.contains('selected'), selected);
    assert.equal(preset.attributes['aria-pressed'], String(selected));
  }
};
for (const [id, threshold] of [['monoPortraitPreset',120], ['monoPcbPreset',225], ['monoObjectsPreset',170]]) {
  controller.configureJob();
  assert.equal(elements.get('saveButton').disabled, false);
  const before = timers.length;
  elements.get(id).listeners.click();
  const result = controller.snapshot();
  assert.equal(Number(result.threshold), threshold);
  assert.equal(elements.get('monoThresholdOutput').textContent, String(threshold));
  assert.equal(result.paletteMode, 'mono');
  assert.equal(result.dirty, true);
  assert.equal(result.previewMode, 'edit');
  assert.equal(elements.get('saveButton').disabled, true);
  assert.equal(elements.get('jobStatus').textContent, 'SETTINGS CHANGED');
  assert.equal(timers.length, before + 1);
  assert.equal(timers.at(-1).delay, 100);
  expectSelected(threshold);
  const saved = JSON.parse(storage.get('citadela.settings'));
  assert.equal(Number(saved.mono_threshold), threshold);
  assert.equal(saved.palette_mode, 'mono');
}
const slider = elements.get('mono_threshold');
slider.value = '171'; slider.listeners.input();
expectSelected(null);
assert.equal(elements.get('monoThresholdOutput').textContent, '171');
assert.equal(Number(JSON.parse(storage.get('citadela.settings')).mono_threshold), 171);
slider.value = '225'; slider.listeners.input();
expectSelected(225);
// Restoring a saved exact preset also restores its highlight.
slider.value = '120';
controller.restore();
assert.equal(Number(slider.value), 225);
expectSelected(225);
console.log('Monochrome presets, manual thresholds, refresh, invalidation, and persistence verified.');
