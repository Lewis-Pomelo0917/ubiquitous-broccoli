import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../..', import.meta.url));
const html = await readFile(join(root, 'development', 'frontend', 'index.html'), 'utf8');

const declaration = (name) => {
  const start = html.indexOf(`const ${name} =`);
  if (start < 0) throw new Error(`找不到实际声明: ${name}`);
  let depth = 0;
  let quote = '';
  let escaped = false;
  for (let index = html.indexOf('=', start) + 1; index < html.length; index += 1) {
    const char = html[index];
    if (quote) {
      if (escaped) escaped = false;
      else if (char === '\\') escaped = true;
      else if (char === quote) quote = '';
      continue;
    }
    if (char === '"' || char === "'" || char === '`') { quote = char; continue; }
    if (char === '{') depth += 1;
    else if (char === '}') depth -= 1;
    else if (char === ';' && depth === 0) return html.slice(start, index + 1);
  }
  throw new Error(`实际声明未闭合: ${name}`);
};

const evaluate = (names, globals, exports) => {
  const context = { ...globals };
  vm.createContext(context);
  vm.runInContext(`${names.map(declaration).join('\n')}\nglobalThis.__exports = { ${exports.join(', ')} };`, context);
  return { context, api: context.__exports };
};

const calc = evaluate(
  ['formatCalcValue', 'parseCalcExpression', 'appendCalcValue', 'toggleCalcSign'],
  { calcOperators: new Set(['+', '−', '×', '÷']), calcExpression: '', calcLastNumeric: null,
    calcJustEvaluated: false, calcCopyStatusElement: { textContent: '' }, renderCalc: () => {} },
  ['formatCalcValue', 'parseCalcExpression', 'appendCalcValue', 'toggleCalcSign']
);
const press = (value) => calc.api.appendCalcValue(value);
['1', '+', '1'].forEach(press);
calc.context.calcLastNumeric = calc.api.parseCalcExpression(calc.context.calcExpression);
calc.context.calcJustEvaluated = true;
press('+');
press('3');
assert.equal(calc.api.parseCalcExpression(calc.context.calcExpression), 5, '计算器连续计算结果错误');
calc.context.calcExpression = '';
calc.context.calcLastNumeric = null;
calc.context.calcJustEvaluated = false;
['5', '−', '3'].forEach(press);
calc.api.toggleCalcSign();
assert.equal(calc.api.parseCalcExpression(calc.context.calcExpression), 8, '计算器 ± 未识别减法末项');
calc.api.toggleCalcSign();
assert.equal(calc.api.parseCalcExpression(calc.context.calcExpression), 2, '计算器 ± 第二次切换错误');
calc.context.calcExpression = '';
['1', '÷', '1', '0', '0', '0', '0', '0', '0', '0'].forEach(press);
calc.context.calcLastNumeric = calc.api.parseCalcExpression(calc.context.calcExpression);
calc.context.calcJustEvaluated = true;
press('+');
press('1');
assert.equal(calc.api.parseCalcExpression(calc.context.calcExpression), 1.0000001, '计算器科学记数法连续计算错误');
const calcCopyBlock = html.slice(html.indexOf("document.getElementById('calc-copy-btn')"), html.indexOf("document.addEventListener('keydown'"));
assert.match(calcCopyBlock, /parseCalcExpression\(calcExpression\)/);
assert.doesNotMatch(calcCopyBlock, /calcLastNumeric === null \?/);

const memo = evaluate(
  ['memoNow', 'memoNormalizeDate', 'memoNormalizeCategory', 'memoParseTags', 'memoNewId', 'memoValidTimestamp', 'memoClamp', 'normalizeMemoRecord', 'parseMemoBackup'],
  { memoDefaultCategory: '未分类', memoStatusValues: ['active', 'done', 'archived'],
    memoLimits: { title: 200, content: 50000, category: 80, tag: 80, tags: 20 },
    crypto: { randomUUID: () => 'regression-id' } },
  ['parseMemoBackup']
);
const largeContent = 'x'.repeat(50001);
const imported = memo.api.parseMemoBackup(JSON.stringify({ schemaVersion: 2, notes: [{
  id: 'source-id', title: 'large', content: largeContent, category: 'A',
  tags: Array.from({ length: 21 }, (_, index) => `tag-${index + 1}`), date: '', status: 'active', pinned: false,
  createdAt: '2026-09-07T00:00:00.000Z', updatedAt: '2026-09-07T00:00:00.000Z'
}] }));
assert.equal(imported.notes[0].content.length, 50001, '备忘录导入截断正文');
assert.equal(imported.notes[0].tags.length, 21, '备忘录导入截断标签');
const exportBlock = html.slice(html.indexOf('const exportMemos'), html.indexOf('const parseMemoBackup'));
assert.match(exportBlock, /const saved = flushMemoSave\(\)/);
assert.match(exportBlock, /尚未保存/);
const copyMemoBlock = html.slice(html.indexOf("memoCopyButton.addEventListener"), html.indexOf("memoDeleteButton.addEventListener"));
assert.match(copyMemoBlock, /const saved = flushMemoSave\(\)/);
assert.match(copyMemoBlock, /尚未保存/);
const filterBlock = html.slice(html.indexOf('const renderMemoFilters'), html.indexOf('const memoDaysInMonth'));
assert.match(filterBlock, /memoFilters\.category = ''/);
assert.match(filterBlock, /memoFilters\.tag = ''/);
assert.doesNotMatch(html, /备忘(?!录)/);

const makeMemoListTestNode = () => ({
  children: [],
  attributes: {},
  dataset: {},
  className: '',
  textContent: '',
  type: '',
  get firstChild() { return this.children[0] || null; },
  get childElementCount() { return this.children.length; },
  appendChild(child) { this.children.push(child); return child; },
  append(...children) { children.forEach((child) => this.appendChild(child)); },
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); },
  setAttribute(name, value) { this.attributes[name] = value; }
});
const memoListTestState = { notes: [] };
const memoListTestOutput = makeMemoListTestNode();
const memoListTestCount = makeMemoListTestNode();
const memoListTest = evaluate(['renderMemoList'], {
  memoState: memoListTestState,
  memoSelectedId: null,
  memoList: memoListTestOutput,
  memoResultCount: memoListTestCount,
  getFilteredMemos: () => memoListTestState.notes,
  clearMemoElement: (element) => { element.children = []; },
  formatMemoTime: () => '测试时间',
  document: { createElement: () => makeMemoListTestNode(), createDocumentFragment: () => makeMemoListTestNode() }
}, ['renderMemoList']);
const memoListText = (node) => {
  const values = [node.textContent];
  node.children.forEach((child) => values.push(...memoListText(child)));
  return values;
};
memoListTest.context.memoState.notes = [{ id: 'blank-title', title: '', content: '', category: '客户', tags: [], date: '', status: 'active', pinned: false, updatedAt: '2026-09-10T00:00:00.000Z' }];
memoListTest.api.renderMemoList();
assert.equal(memoListTestCount.textContent, '1 条备忘录', '备忘录列表计数文案错误');
assert.ok(memoListText(memoListTestOutput).includes('无标题备忘录'), '空标题列表回退文案错误');
assert.equal(memoListTestOutput.children[0].children[0].children[0].attributes['aria-label'], '打开 无标题备忘录', '空标题无障碍标签错误');
memoListTest.context.memoState.notes = [];
memoListTest.api.renderMemoList();
assert.equal(memoListTestCount.textContent, '0 条备忘录', '空列表计数文案错误');
assert.ok(memoListText(memoListTestOutput).includes('还没有备忘录'), '空列表状态文案错误');
assert.ok(memoListText(memoListTestOutput).includes('新建第一条备忘录'), '空列表按钮文案错误');
memoListTest.context.memoState.notes = [{ id: 'filtered', title: '测试', content: '', category: '客户', tags: [], date: '', status: 'active', pinned: false, updatedAt: '2026-09-10T00:00:00.000Z' }];
memoListTest.context.getFilteredMemos = () => [];
memoListTest.api.renderMemoList();
assert.ok(memoListText(memoListTestOutput).includes('没有符合条件的备忘录'), '无匹配列表状态文案错误');
assert.ok(memoListText(memoListTestOutput).includes('清除筛选'), '无匹配列表按钮文案错误');

const categoryFocus = { current: '' };
const makeCategorySelect = () => ({
  value: '',
  options: [],
  get firstChild() { return this.options[0] || null; },
  appendChild(option) { this.options.push(option); },
  removeChild() { this.options.shift(); },
  focus() { categoryFocus.current = 'select'; }
});
const categorySelect = makeCategorySelect();
const categoryCustomInput = {
  value: '',
  focus() { categoryFocus.current = 'custom'; },
  addEventListener(event, handler) { this.handlers = this.handlers || {}; this.handlers[event] = handler; }
};
const categoryCustomPanel = { hidden: true };
const categoryCustomToggle = {
  attributes: {},
  setAttribute(name, value) { this.attributes[name] = value; },
  focus() { categoryFocus.current = 'toggle'; }
};
const categoryCustomNote = { className: '', textContent: '' };
const categoryCustomEvents = [];
categorySelect.dispatchEvent = (event) => categoryCustomEvents.push(event.type);
const importedLongCategory = `旧导入分类-${'x'.repeat(90)}`;
const categoryMemoState = { notes: [{ category: '客户' }, { category: '自定义分类' }, { category: importedLongCategory }, { category: '自定义分类' }] };
const categoryFunctions = evaluate(['memoCategoryPresets', 'fillMemoCategorySelect', 'setMemoCategoryCustomOpen', 'applyMemoCustomCategory'], {
  memoState: categoryMemoState,
  memoDefaultCategory: '未分类',
  memoEditorCategory: categorySelect,
  clearMemoElement: (element) => { while (element.firstChild) element.removeChild(element.firstChild); },
  document: { createElement: () => ({ value: '', textContent: '' }) },
  memoCategoryCustomToggle: categoryCustomToggle,
  memoCategoryCustomPanel: categoryCustomPanel,
  memoCategoryCustomInput: categoryCustomInput,
  memoCategoryCustomNote: categoryCustomNote,
  memoLimits: { category: 80 },
  Event: class { constructor(type, init = {}) { this.type = type; Object.assign(this, init); } }
}, ['fillMemoCategorySelect', 'setMemoCategoryCustomOpen', 'applyMemoCustomCategory']);
categoryFunctions.api.fillMemoCategorySelect(importedLongCategory);
const categoryOptionValues = categoryFunctions.context.memoEditorCategory.options.map((option) => option.value);
assert.deepEqual(categoryOptionValues.slice(0, 8), ['未分类', '客户', '订单', '报价', '生产', '出货', '待办', '其他'], '分类预设选项变化');
assert.ok(categoryOptionValues.includes('自定义分类'), '现有自定义分类未加入选项');
assert.ok(categoryOptionValues.includes(importedLongCategory), '旧导入长分类被截断或丢失');
assert.equal(categoryFunctions.context.memoEditorCategory.value, importedLongCategory, '当前分类未正确回显');
categoryFunctions.api.setMemoCategoryCustomOpen(true);
assert.equal(categoryCustomPanel.hidden, false, '自定义分类面板未展开');
assert.equal(categoryCustomToggle.attributes['aria-expanded'], 'true', '自定义分类展开状态未同步');
assert.equal(categoryFocus.current, 'custom', '展开自定义分类后未聚焦输入框');
categoryCustomInput.value = '  ';
categoryFunctions.api.applyMemoCustomCategory();
assert.match(categoryCustomNote.className, /is-error/);
assert.equal(categoryCustomPanel.hidden, false, '空白分类错误时面板被关闭');
assert.equal(categoryCustomEvents.length, 0, '空白分类错误时错误触发保存');
categoryFunctions.api.setMemoCategoryCustomOpen(true);
categoryCustomInput.value = 'x'.repeat(81);
categoryFunctions.api.applyMemoCustomCategory();
assert.match(categoryCustomNote.textContent, /最多 80 个字符/);
assert.equal(categoryCustomEvents.length, 0, '超长分类错误时错误触发保存');
categoryFunctions.api.setMemoCategoryCustomOpen(true);
categoryCustomInput.value = '客户/订单#1';
categoryFunctions.api.applyMemoCustomCategory();
assert.equal(categoryFunctions.context.memoEditorCategory.value, '客户/订单#1', '特殊字符自定义分类未应用');
assert.equal(categoryFunctions.context.memoEditorCategory.options.filter((option) => option.value === '客户/订单#1').length, 1, '自定义分类选项未去重');
assert.equal(categoryFocus.current, 'select', '自定义分类应用后焦点未回到 select');
categoryFunctions.api.setMemoCategoryCustomOpen(true);
categoryCustomInput.value = '客户';
categoryFunctions.api.applyMemoCustomCategory();
assert.equal(categoryFunctions.context.memoEditorCategory.options.filter((option) => option.value === '客户').length, 1, '重名分类产生重复选项');

const memoCategoryKeydownBlock = html.slice(html.indexOf("memoCategoryCustomInput.addEventListener('keydown'"), html.indexOf("memoEditorStatus.addEventListener('change'"));
vm.runInContext(memoCategoryKeydownBlock, categoryFunctions.context, { filename: 'outputs/index.html' });
categoryFunctions.context.memoCategoryCustomCancel = { click: () => categoryFunctions.api.setMemoCategoryCustomOpen(false) };
categoryFunctions.api.setMemoCategoryCustomOpen(true);
categoryCustomInput.value = '组合输入分类';
let composingPrevented = false;
categoryCustomInput.handlers.keydown({ key: 'Enter', isComposing: true, preventDefault: () => { composingPrevented = true; } });
assert.equal(composingPrevented, false, '输入法组合中的 Enter 被错误拦截');
assert.equal(categoryCustomPanel.hidden, false, '输入法组合中的 Enter 错误关闭面板');
assert.notEqual(categoryFunctions.context.memoEditorCategory.value, '组合输入分类', '输入法组合中的 Enter 错误应用分类');
let enterPrevented = false;
categoryCustomInput.handlers.keydown({ key: 'Enter', isComposing: false, preventDefault: () => { enterPrevented = true; } });
assert.equal(enterPrevented, true, '普通 Enter 未阻止默认行为');
assert.equal(categoryFunctions.context.memoEditorCategory.value, '组合输入分类', '普通 Enter 未应用分类');
assert.equal(categoryFocus.current, 'select', '普通 Enter 应用后焦点未回到 select');

const editorNote = { id: 'memo-1', title: '标题', content: '正文', category: '客户', tags: ['旧标签'], date: '2026-09-10', status: 'active', updatedAt: '旧时间' };
const editorCategory = { value: '订单', handlers: {}, addEventListener(event, handler) { this.handlers[event] = handler; }, focus() {} };
const editorFields = {
  title: { value: editorNote.title }, content: { value: editorNote.content }, tags: { value: '旧标签' }, date: { value: editorNote.date }, status: { value: 'active' }
};
let editorSaveStatus = null;
let editorStorageCalls = 0;
let editorStorageKey = '';
let editorStoragePayload = '';
const memoEditor = evaluate(['memoNow', 'memoNormalizeDate', 'memoNormalizeCategory', 'memoParseTags', 'saveMemoState', 'updateMemoFromEditor', 'flushMemoSave'], {
  memoKey: 'tradeDesk.memos.v2',
  memoState: { schemaVersion: 2, notes: [editorNote] },
  selectedMemo: () => editorNote,
  memoEditorForm: { hidden: false },
  memoEditorTitleInput: editorFields.title,
  memoEditorContent: editorFields.content,
  memoEditorCategory: editorCategory,
  memoEditorTags: editorFields.tags,
  memoEditorDate: editorFields.date,
  memoEditorStatus: editorFields.status,
  memoStorageBlocked: false,
  memoSaveTimer: null,
  memoSavePending: false,
  window: { clearTimeout: () => {} },
  localStorage: { setItem: (key, payload) => { editorStorageCalls += 1; editorStorageKey = key; editorStoragePayload = payload; throw new Error('simulated save failure'); } },
  setMemoStatus: (text, isError) => { editorSaveStatus = { text, isError }; },
  formatMemoTime: () => ''
}, ['updateMemoFromEditor', 'flushMemoSave']);
const memoCategoryChangeBlock = html.slice(html.indexOf("memoEditorCategory.addEventListener('change'"), html.indexOf("memoCategoryCustomToggle.addEventListener"));
let editorScheduled = 0;
let editorFilterRenders = 0;
let editorCalendarRenders = 0;
let editorListRenders = 0;
memoEditor.context.scheduleMemoSave = () => { editorScheduled += 1; };
memoEditor.context.renderMemoFilters = () => { editorFilterRenders += 1; };
memoEditor.context.renderMemoCalendar = () => { editorCalendarRenders += 1; };
memoEditor.context.renderMemoList = () => { editorListRenders += 1; };
vm.runInContext(memoCategoryChangeBlock, memoEditor.context, { filename: 'outputs/index.html' });
editorCategory.handlers.change();
assert.equal(editorNote.category, '订单', '原生 select change 未更新分类');
assert.equal(editorNote.title, '标题', '分类改变误改标题');
assert.equal(editorNote.content, '正文', '分类改变误改正文');
assert.equal(JSON.stringify(editorNote.tags), JSON.stringify(['旧标签']));
assert.equal(editorScheduled, 1, '分类改变未进入自动保存链');
assert.equal(editorFilterRenders, 1);
assert.equal(editorCalendarRenders, 1);
assert.equal(editorListRenders, 1);
assert.equal(memoEditor.api.flushMemoSave(), false, '模拟保存失败未返回失败');
assert.equal(editorStorageCalls, 1, '模拟保存失败未调用 localStorage.setItem');
assert.equal(editorStorageKey, 'tradeDesk.memos.v2', '模拟保存写入了错误的存储键');
assert.equal(JSON.parse(editorStoragePayload).notes[0].category, '订单', '模拟保存 payload 未包含新分类');
assert.equal(editorNote.category, '订单', '保存失败时分类被错误清除');
assert.equal(editorSaveStatus.isError, true, '模拟保存失败未显示错误状态');
assert.match(editorSaveStatus.text, /保存失败/);
assert.match(html, /<select class="memo-editor-control" id="memo-editor-category"/);
assert.doesNotMatch(html, /id="memo-editor-category"[^>]*list=/);

const makeAdvancedElement = (value = '') => ({
  value: String(value), textContent: '', className: '', style: { color: '' },
  dataset: {}, classList: { toggle: () => {} }, closest: () => ({ classList: { toggle: () => {} } })
});
const advancedValues = new Map(Object.entries({
  'book-length': 21, 'book-width': 14.8, 'page-count': 80, 'page-mode': 'page',
  'inner-material': 'double-glue', 'inner-gsm': 80, 'inner-caliper': 0.1,
  'front-material': 'c1s', 'front-gsm': 250, 'front-thickness': 0.25, 'front-thickness-unit': 'mm', 'front-unit-weight': '',
  'back-material': 'c1s', 'back-gsm': 250, 'back-thickness': 0.25, 'back-thickness-unit': 'mm', 'back-unit-weight': '',
  'pp-density': 0.91, binding: 'saddle', 'binding-allowance': 0.3, 'binding-extra-weight': 0
}));
const advancedElements = new Map([...advancedValues].map(([id, value]) => [id, makeAdvancedElement(value)]));
for (const id of ['book-thickness', 'book-volume', 'book-single-cbm', 'book-sheets', 'book-inner-thickness',
  'book-cover-thickness', 'book-binding-thickness', 'advanced-result-formula', 'advanced-result-state',
  'book-net-weight', 'book-net-weight-kg', 'advanced-weight-state', 'advanced-weight-note', 'advanced-weight-transfer-status',
  'book-inner-weight', 'book-front-weight', 'book-back-weight', 'book-extra-weight', 'sheet-note', 'inner-caliper-note',
  'advanced-accuracy-note', 'front-thickness-tools', 'back-thickness-tools', 'front-thickness-mm-note',
  'back-thickness-mm-note', 'pp-density-row']) {
  advancedElements.set(id, makeAdvancedElement());
}
advancedElements.set('advanced-weight-hero', makeAdvancedElement());
const advancedDocument = { getElementById: (id) => advancedElements.get(id) };
const advancedOutput = {
  thickness: advancedElements.get('book-thickness'), volume: advancedElements.get('book-volume'),
  singleCbm: advancedElements.get('book-single-cbm'), sheets: advancedElements.get('book-sheets'),
  innerThickness: advancedElements.get('book-inner-thickness'), coverThickness: advancedElements.get('book-cover-thickness'),
  bindingThickness: advancedElements.get('book-binding-thickness'), formula: advancedElements.get('advanced-result-formula'),
  state: advancedElements.get('advanced-result-state'), weightHero: advancedElements.get('advanced-weight-hero'),
  netWeight: advancedElements.get('book-net-weight'), netWeightKg: advancedElements.get('book-net-weight-kg'),
  weightState: advancedElements.get('advanced-weight-state'), weightNote: advancedElements.get('advanced-weight-note'),
  innerWeight: advancedElements.get('book-inner-weight'), frontWeight: advancedElements.get('book-front-weight'),
  backWeight: advancedElements.get('book-back-weight'), extraWeight: advancedElements.get('book-extra-weight'),
  accuracyNote: advancedElements.get('advanced-accuracy-note'), copyStatus: makeAdvancedElement(),
  weightTransferStatus: advancedElements.get('advanced-weight-transfer-status'),
  sheetNote: advancedElements.get('sheet-note'), innerCaliperNote: advancedElements.get('inner-caliper-note')
};
const advanced = evaluate(
  ['format', 'rawValue', 'paperFactors', 'paperNames', 'coverFactors', 'paperCoverMaterials', 'thicknessUnitFactor',
    'stableThicknessValue', 'readThicknessMm', 'writeThicknessMm', 'convertThicknessUnit', 'syncThicknessNote',
    'syncCoverMaterialUi', 'syncPpDensityVisibility', 'syncCoverRecommended', 'readAdvancedInput', 'isPositiveFinite',
    'formatWeight', 'formatWeightDetail', 'writeAdvancedWeightToCbm', 'calculateAdvanced'],
  { document: advancedDocument, advancedOutput, latestAdvancedWeight: null },
  ['calculateAdvanced', 'readThicknessMm', 'convertThicknessUnit', 'syncCoverMaterialUi', 'syncCoverRecommended', 'syncPpDensityVisibility', 'writeAdvancedWeightToCbm']
);
const setAdvancedValue = (id, value) => { advancedElements.get(id).value = String(value); };
const assertApprox = (actual, expected, message) => assert.ok(Math.abs(actual - expected) < 1e-9, `${message}: ${actual}`);
let advancedResult = advanced.api.calculateAdvanced();
assertApprox(advancedResult.weight.grams, 114.996, '本册默认单本重量错误');
assert.equal(advancedElements.get('book-net-weight').textContent, '114.996 g');
assert.equal(advancedElements.get('book-net-weight-kg').textContent, '0.114996 kg');
assert.equal(advancedElements.get('book-volume').textContent, '149.184');
const transferTarget = { value: '0.234' };
assert.equal(advanced.api.writeAdvancedWeightToCbm(advancedResult.weight, transferTarget), true, '有效本册重量未能带入 CBM');
assert.equal(transferTarget.value, String(Number(advancedResult.weight.kg.toPrecision(15))), '带入值未使用计算出的 kg 原始值');
transferTarget.value = '0.234';
assert.equal(advanced.api.writeAdvancedWeightToCbm({ complete: false, kg: NaN }, transferTarget), false, '无效重量不应带入 CBM');
assert.equal(transferTarget.value, '0.234', '无效重量错误覆盖了原有 CBM 重量');
setAdvancedValue('binding-extra-weight', 1);
const changedAdvancedResult = advanced.api.calculateAdvanced();
assert.notEqual(changedAdvancedResult.weight.kg, advancedResult.weight.kg, '参数变化未刷新本册重量');
assert.equal(advanced.api.writeAdvancedWeightToCbm(changedAdvancedResult.weight, transferTarget), true, '最新本册重量未能带入 CBM');
assert.equal(transferTarget.value, String(Number(changedAdvancedResult.weight.kg.toPrecision(15))), '带入值不是参数变化后的最新重量');
setAdvancedValue('binding-extra-weight', 0);
setAdvancedValue('binding-allowance', -1);
assert.equal(advanced.api.calculateAdvanced(), null, '体积无效时原有 calculateAdvanced 返回值被改变');
assert.equal(advanced.context.latestAdvancedWeight.complete, true, '体积无效时未保留有效重量结果');
assert.equal(advanced.api.writeAdvancedWeightToCbm(advanced.context.latestAdvancedWeight, transferTarget), true, '体积无效但重量有效时不应阻止带入');
setAdvancedValue('binding-allowance', 0.3);
advancedResult = advanced.api.calculateAdvanced();
advancedElements.get('front-thickness-unit').dataset.previousValue = 'mm';
advancedElements.get('front-thickness-unit').value = 'c';
advanced.api.convertThicknessUnit('front', 'c', 'mm');
assert.equal(advancedElements.get('front-thickness').value, '25', 'mm 转 C 显示错误');
assertApprox(advanced.api.readThicknessMm('front'), 0.25, 'C 转换为 mm 错误');
advancedElements.get('front-thickness-unit').value = 'mm';
advanced.api.convertThicknessUnit('front', 'mm', 'c');
assert.equal(advancedElements.get('front-thickness').value, '0.25', 'C 转 mm 显示错误');
setAdvancedValue('front-material', 'pp');
setAdvancedValue('back-material', 'pp');
setAdvancedValue('front-thickness-unit', 'c');
setAdvancedValue('back-thickness-unit', 'c');
setAdvancedValue('front-thickness', 30);
setAdvancedValue('back-thickness', 50);
setAdvancedValue('front-unit-weight', '');
setAdvancedValue('back-unit-weight', '');
setAdvancedValue('page-mode', 'sheet');
setAdvancedValue('page-count', 40);
setAdvancedValue('pp-density', 0.91);
setAdvancedValue('binding-extra-weight', 0);
advancedResult = advanced.api.calculateAdvanced();
assertApprox(advancedResult.weight.inner, 99.456, 'PP 样例内页重量错误');
assertApprox(advancedResult.weight.front, 8.48484, 'PP 30C 封面重量错误');
assertApprox(advancedResult.weight.back, 14.1414, 'PP 50C 封底重量错误');
assertApprox(advancedResult.weight.grams, 122.08224, 'PP 混合厚度总重量错误');
assertApprox(advancedResult.totalThickness, 5.1, 'PP 混合厚度总厚度错误');
assertApprox(advancedResult.singleVolume, 158.508, 'PP 混合厚度体积错误');
assert.match(advancedElements.get('book-front-weight').textContent, /PP 厚度估算/);
assert.match(advancedElements.get('book-back-weight').textContent, /PP 厚度估算/);
assert.match(advancedElements.get('advanced-accuracy-note').textContent, /^PP 按成品尺寸和参考密度估算/);
setAdvancedValue('front-gsm', 999);
setAdvancedValue('back-gsm', 999);
assertApprox(advanced.api.calculateAdvanced().weight.grams, 122.08224, 'PP 错误读取残留 GSM');
setAdvancedValue('binding-extra-weight', 2);
advancedResult = advanced.api.calculateAdvanced();
assertApprox(advancedResult.weight.grams, 124.08224, 'PP 附加重量错误');
setAdvancedValue('pp-density', 'bad');
setAdvancedValue('front-unit-weight', 30);
setAdvancedValue('back-unit-weight', 20);
assertApprox(advanced.api.calculateAdvanced().weight.grams, 151.456, 'PP 实测重量优先错误');
setAdvancedValue('front-unit-weight', '');
setAdvancedValue('back-unit-weight', '');
advancedResult = advanced.api.calculateAdvanced();
assert.equal(advancedResult.weight.complete, false, 'PP 无密度时未提示缺项');
assert.equal(advancedElements.get('advanced-weight-note').textContent, 'PP参考密度须为有限正数', 'PP 密度错误提示错误');
assert.equal(advancedElements.get('book-net-weight-kg').textContent, '待补充重量参数');
assert.equal(advancedElements.get('book-net-weight').textContent, '—');
setAdvancedValue('binding-extra-weight', 0);
advancedElements.get('front-thickness-unit').dataset.previousValue = 'c';
advancedElements.get('back-thickness-unit').dataset.previousValue = 'c';
advancedElements.get('front-thickness-unit').value = 'mm';
advancedElements.get('back-thickness-unit').value = 'mm';
advanced.api.convertThicknessUnit('front', 'mm', 'c');
advanced.api.convertThicknessUnit('back', 'mm', 'c');
setAdvancedValue('pp-density', 0.91);
assertApprox(advanced.api.calculateAdvanced().weight.grams, 122.08224, 'PP C 转 mm 后重量变化');
advancedElements.get('front-thickness-unit').value = 'c';
advancedElements.get('back-thickness-unit').value = 'c';
advanced.api.convertThicknessUnit('front', 'c', 'mm');
advanced.api.convertThicknessUnit('back', 'c', 'mm');
assertApprox(advanced.api.calculateAdvanced().weight.grams, 122.08224, 'PP mm 转 C 后重量变化');
setAdvancedValue('front-material', 'c1s');
setAdvancedValue('back-material', 'c1s');
setAdvancedValue('front-gsm', 250);
setAdvancedValue('back-gsm', 250);
advanced.api.syncCoverMaterialUi('front');
advanced.api.syncCoverMaterialUi('back');
advanced.api.syncCoverRecommended('front');
advanced.api.syncCoverRecommended('back');
advanced.api.syncPpDensityVisibility();
assert.equal(advancedElements.get('front-thickness-unit').value, 'mm', '切回纸材未恢复 mm');
assert.equal(advancedElements.get('back-thickness-unit').value, 'mm', '切回纸材未恢复 mm');
assert.equal(advancedElements.get('front-thickness').value, '0.25', '纸材推荐厚度未恢复');
assert.equal(advancedElements.get('back-thickness').value, '0.25', '纸材推荐厚度未恢复');
setAdvancedValue('binding-extra-weight', 0);
setAdvancedValue('page-mode', 'page');
setAdvancedValue('page-count', 80);
setAdvancedValue('front-gsm', 250);
setAdvancedValue('back-gsm', 250);
assertApprox(advanced.api.calculateAdvanced().weight.grams, 114.996, '本册页/张换算错误');
setAdvancedValue('page-mode', 'sheet');
setAdvancedValue('page-count', 80);
assertApprox(advanced.api.calculateAdvanced().weight.grams, 214.452, '本册张数重量错误');
setAdvancedValue('page-mode', 'page');
setAdvancedValue('page-count', 80);
assertApprox(advanced.api.calculateAdvanced().weight.grams, 114.996, '本册页模式重量错误');
setAdvancedValue('page-mode', 'page');
setAdvancedValue('page-count', 81);
setAdvancedValue('binding-extra-weight', 0);
advancedResult = advanced.api.calculateAdvanced();
assert.equal(advancedResult.weight.complete, false, '奇数页不应直接给出完整重量');
assert.match(advancedElements.get('advanced-weight-note').textContent, /奇数页请确认实际纸张数/);
setAdvancedValue('page-count', 80.5);
assert.equal(advanced.api.calculateAdvanced(), null, '非整数内页数量未被拒绝');
assert.equal(advancedElements.get('book-volume').textContent, '—');
const advancedBlock = html.slice(html.indexOf('const calculateAdvanced'), html.indexOf('const currencyData'));
assert.match(advancedBlock, /front-unit-weight/);
assert.match(advancedBlock, /back-unit-weight/);
assert.match(advancedBlock, /binding-extra-weight/);
assert.match(advancedBlock, /readThicknessMm/);
assert.match(advancedBlock, /material === 'pp'/);
assert.match(advancedBlock, /PP 厚度估算/);
assert.match(advancedBlock, /ppDensity/);
assert.match(advancedBlock, /values\.weight\.complete/);
assert.match(html, /id="front-unit-weight"[^>]*placeholder="纸质可留空"/);
assert.match(html, /id="back-unit-weight"[^>]*placeholder="纸质可留空"/);
assert.match(html, /id="binding-extra-weight"[^>]*value="0"/);
assert.match(html, /id="front-thickness-unit"/);
assert.match(html, /id="back-thickness-unit"/);
assert.match(html, /data-c="30"/);
assert.match(html, /data-c="50"/);
assert.match(html, /id="pp-density"[^>]*value="0\.91"/);
assert.match(html, /PP 可按厚度估算/);
assert.match(html, /按成品尺寸估算，不含外包装；封面出边、书脊、包边及未填写的附加材料未计入，以样品实称为准。/);
assert.match(html, /PP 按成品尺寸和参考密度估算，未计封面出边、印刷及附件，以样品实称为准。/);
assert.match(html, /id="advanced-weight-to-cbm"/);
assert.match(html, /id="advanced-weight-transfer-status"/);
assert.match(html, /id="book-weight"[^>]*step="any"/);
assert.match(html, /Single net weight: \$\{format\(values\.weight\.kg, 8\)\} kg/);
const advancedWeightTransferBlock = html.slice(html.indexOf("document.getElementById('advanced-weight-to-cbm')"), html.indexOf("document.getElementById('advanced-clear-btn')"));
assert.match(advancedWeightTransferBlock, /values\?\.weight \|\| latestAdvancedWeight/);
assert.match(advancedWeightTransferBlock, /document\.getElementById\('tab-volume'\)\.click\(\)/);
assert.match(advancedWeightTransferBlock, /scrollIntoView/);
const advancedClearBlock = html.slice(html.indexOf("document.getElementById('advanced-clear-btn')"), html.indexOf("document.getElementById('advanced-copy-btn')"));
assert.match(advancedClearBlock, /advanced-form'\)\.reset\(\)/);

const fields = new Map(Object.entries({ length: 50, width: 40, height: 30, quantity: 100, 'total-books': 4500,
  'carton-pieces': 50, 'book-weight': 0.2, 'pack-carton-tare': 1 }));
const packingOutput = Object.fromEntries(['recommended', 'adopted', 'cartons', 'tail', 'singleCbm', 'totalCbm', 'totalNet', 'totalGross', 'note']
  .map((key) => [key, { textContent: '' }]));
const packing = evaluate(['format', 'dimensionPermutations', 'calculatePacking'], {
  unitFactor: { mm: 0.001, cm: 0.01, m: 1, in: 0.0254 }, unit: { value: 'cm' },
  rawValue: (id) => Number(fields.get(id)), optionalRawValue: (id) => fields.has(id) ? Number(fields.get(id)) : NaN,
  syncDimensionUnitLabels: () => {}, clearPackingOutputs: () => {}, packingOutput
}, ['calculatePacking']);
const packingResult = packing.api.calculatePacking();
assert.equal(packingResult.actualCartons, 100, '装柜估算未读取实际箱数');
assert.equal(packingResult.totalCbm, 6, '装柜估算总体积错误');
assert.equal(packingResult.totalGross, 1000, '装柜估算毛重错误');

const amount = evaluate(['currencyData', 'parseAmount'], {}, ['currencyData', 'parseAmount']);
assert.equal(Object.keys(amount.api.currencyData).length, 11, '金额币种数量变化');
const usd = amount.api.currencyData.USD;
const jpy = amount.api.currencyData.JPY;
const groupedAmount = amount.api.parseAmount('1,234.56', usd, 'round');
assert.equal(groupedAmount.integerString, '1234', '规范千分位金额整数解析错误');
assert.equal(groupedAmount.minorString, '56', '规范千分位金额小数解析错误');
assert.equal(amount.api.parseAmount('1234.56', usd, 'round').integerString, '1234', '未分组金额解析错误');
assert.equal(amount.api.parseAmount('$-20.5', usd, 'round').negative, true, '负数或币种符号解析错误');
assert.equal(amount.api.parseAmount('0.05', usd, 'round').minorString, '05', '0.05 金额解析错误');
assert.equal(amount.api.parseAmount('.5', usd, 'round').minorString, '50', '.5 金额解析错误');
for (const raw of ['1.234,56', '12,34.56', '1,23']) {
  assert.ok(amount.api.parseAmount(raw, usd, 'round').error, `错误金额格式未被拒绝: ${raw}`);
}
assert.equal(amount.api.parseAmount('1.234', usd, 'round').minorString, '23', '向下金额舍入错误');
assert.equal(amount.api.parseAmount('1.234', usd, 'round').rounded, true, '向下金额舍入未标记');
assert.equal(amount.api.parseAmount('1.235', usd, 'round').minorString, '24', '向上金额舍入错误');
assert.equal(amount.api.parseAmount('1.235', usd, 'round').rounded, true, '向上金额舍入未标记');
assert.equal(amount.api.parseAmount('1.2300', usd, 'round').rounded, false, '尾随零被误标为舍入');
assert.ok(amount.api.parseAmount('1.234', usd, 'strict').error, '严格模式未拒绝超位非零小数');
assert.equal(amount.api.parseAmount('1.2300', usd, 'strict').rounded, false, '严格模式误标记尾随零');
assert.ok(amount.api.parseAmount('999999999999999.999', usd, 'round').error, '舍入进位后超 15 位未拒绝');
assert.equal(amount.api.parseAmount('-123', jpy, 'round').negative, true, 'JPY 负数解析错误');
assert.equal(amount.api.parseAmount('1234.4', jpy, 'round').integerString, '1234', 'JPY 向下舍入错误');
assert.equal(amount.api.parseAmount('1234.4', jpy, 'round').rounded, true, 'JPY 超位舍入未标记');
assert.equal(amount.api.parseAmount('1234.5', jpy, 'round').integerString, '1235', 'JPY 进位边界错误');
assert.ok(amount.api.parseAmount('999999999999999.5', jpy, 'round').error, 'JPY 舍入进位后超 15 位未拒绝');

const amountElementIds = ['amount-value', 'amount-currency', 'amount-rounding', 'amount-english', 'amount-case', 'amount-format',
  'amount-primary', 'amount-secondary', 'amount-secondary-label', 'amount-parsed', 'amount-normalized', 'amount-currency-label',
  'amount-major', 'amount-minor', 'amount-minor-label', 'amount-status-short', 'amount-result-state', 'amount-state-note', 'amount-copy-status'];
const amountElements = new Map(amountElementIds.map((id) => [id, { value: '', textContent: '', style: { color: '' } }]));
amountElements.get('amount-currency').value = 'USD';
amountElements.get('amount-rounding').value = 'round';
amountElements.get('amount-english').value = 'american';
amountElements.get('amount-case').value = 'upper';
amountElements.get('amount-format').value = 'words';
const amountUi = evaluate([
  'currencyData', 'amountUnits', 'amountOutput', 'groupThousands', 'threeDigitsToWords', 'integerToWords',
  'parseAmount', 'applyCase', 'makeInvoiceWords', 'makeFullWords', 'calculateAmount'
], { document: { getElementById: (id) => amountElements.get(id) } }, ['calculateAmount']);
amountElements.get('amount-value').value = '1.234';
amountUi.api.calculateAmount();
assert.equal(amountElements.get('amount-status-short').textContent, '已四舍五入', '金额向下舍入状态提示错误');
assert.equal(amountElements.get('amount-result-state').textContent, '已转换 · 已四舍五入', '金额舍入结果状态提示错误');
amountElements.get('amount-value').value = '1.2300';
amountUi.api.calculateAmount();
assert.equal(amountElements.get('amount-status-short').textContent, '有效', '尾随零金额状态误报');
assert.equal(amountElements.get('amount-result-state').textContent, '已转换', '尾随零金额结果状态误报');

const containerClickBlock = html.slice(html.indexOf("document.getElementById('container-use-result')"), html.indexOf("document.getElementById('container-clear-btn')"));
let containerClickHandler;
const containerUseButton = {
  addEventListener: (event, handler) => {
    assert.equal(event, 'click', '装柜带入事件类型错误');
    containerClickHandler = handler;
  }
};
const containerInputs = new Map([
  ['container-use-result', containerUseButton],
  ['container-total-cbm', { value: '270' }],
  ['container-total-gross', { value: '100000' }]
]);
const containerClickState = { values: null, packing: null, planCalls: 0, cleared: [] };
const containerClickContext = {
  document: { getElementById: (id) => containerInputs.get(id) },
  calculate: () => containerClickState.values,
  calculatePacking: () => containerClickState.packing,
  calculateContainerPlans: () => { containerClickState.planCalls += 1; },
  clearContainerPlans: (message) => { containerClickState.cleared.push(message); },
  containerPlanOutput: { inputNote: { textContent: '' } }
};
vm.createContext(containerClickContext);
vm.runInContext(containerClickBlock, containerClickContext, { filename: 'outputs/index.html' });
assert.equal(typeof containerClickHandler, 'function', '装柜带入按钮未注册');
const clickContainer = (values, packing, cbm = '270', gross = '100000') => {
  containerClickState.values = values;
  containerClickState.packing = packing;
  containerInputs.get('container-total-cbm').value = cbm;
  containerInputs.get('container-total-gross').value = gross;
  containerClickState.cleared.length = 0;
  containerClickHandler();
};
clickContainer({ totalCbm: 6 }, { totalGross: 1000 }, '', '');
assert.equal(containerInputs.get('container-total-cbm').value, '6', '完整结果未带入 CBM');
assert.equal(containerInputs.get('container-total-gross').value, '1000', '完整结果未带入 G.W.');
assert.equal(containerClickState.planCalls, 1, '完整结果未恢复装柜计算');
clickContainer({ totalCbm: 6 }, { totalGross: NaN });
assert.equal(containerInputs.get('container-total-cbm').value, '6', '缺毛重时有效 CBM 未保留');
assert.equal(containerInputs.get('container-total-gross').value, '', '缺毛重时仍保留旧毛重');
assert.match(containerClickState.cleared.at(-1), /缺少有效 G\.W\./, '缺毛重时未明确提示');
assert.equal(containerClickState.planCalls, 1, '缺毛重时错误恢复旧推荐');
clickContainer({ totalCbm: NaN }, { totalGross: 1200 });
assert.equal(containerInputs.get('container-total-cbm').value, '', '缺 CBM 时仍保留旧体积');
assert.equal(containerInputs.get('container-total-gross').value, '1200', '缺 CBM 时有效 G.W. 未保留');
assert.match(containerClickState.cleared.at(-1), /缺少有效 CBM/, '缺 CBM 时未明确提示');
clickContainer(null, null);
assert.equal(containerInputs.get('container-total-cbm').value, '', '两项无效时仍保留旧 CBM');
assert.equal(containerInputs.get('container-total-gross').value, '', '两项无效时仍保留旧 G.W.');
assert.match(containerClickState.cleared.at(-1), /CBM.*G\.W\./, '两项无效时未明确提示');

const containerPlanTypes = ['20GP', '40GP', '40HQ'];
const planRowFields = ['label', 'tag', 'basis', 'volumeCount', 'weightCount', 'requiredCount', 'volumeUtilization', 'weightUtilization', 'remainingVolume', 'remainingPayload', 'limiting'];
const makePlanElement = () => ({ textContent: '', value: '', hidden: false, classList: { remove: () => {}, toggle: () => {} } });
const planInputs = new Map([
  ['container-total-cbm', makePlanElement()],
  ['container-total-gross', makePlanElement()]
]);
const planRows = Object.fromEntries(containerPlanTypes.map((type) => [type, {
  row: makePlanElement(),
  ...Object.fromEntries(planRowFields.map((field) => [field, makePlanElement()]))
}]));
const planOutput = { value: makePlanElement(), tag: makePlanElement(), note: makePlanElement(), inputNote: makePlanElement(), rows: planRows };
const containerPlans = evaluate(['containerProfiles', 'calculateContainerPlan', 'planInputValue', 'clearContainerPlans', 'calculateContainerPlans'], {
  document: { getElementById: (id) => planInputs.get(id) },
  rawValue: (id) => Number(planInputs.get(id).value),
  format: (value, digits) => Number(value).toFixed(digits),
  containerPlanOutput: planOutput
}, ['calculateContainerPlans']);
planInputs.get('container-total-cbm').value = '6';
planInputs.get('container-total-gross').value = '';
assert.equal(containerPlans.api.calculateContainerPlans(), null, '手工补齐前缺项未保持等待状态');
planInputs.get('container-total-gross').value = '1000';
assert.equal(containerPlans.api.calculateContainerPlans().recommended.type, '20GP', '手工补齐后未恢复推荐计算');
assert.equal(planOutput.value.textContent, '1 × 20GP', '手工补齐后推荐显示错误');

const amountCopyBlock = html.slice(html.indexOf("document.getElementById('amount-copy-btn')"), html.indexOf('const plainNumberInput'));
let amountCopyHandler;
let copiedAmount = '';
const amountCopyButton = { addEventListener: (event, handler) => { assert.equal(event, 'click'); amountCopyHandler = handler; } };
const amountCopyStatus = { className: '', textContent: '' };
const amountCopyState = { values: { primary: 'PRIMARY', secondary: 'SECONDARY' } };
const amountCopyContext = {
  document: { getElementById: (id) => id === 'amount-copy-btn' ? amountCopyButton : null },
  calculateAmount: () => amountCopyState.values,
  amountOutput: { copyStatus: amountCopyStatus },
  navigator: { clipboard: { writeText: async (text) => { copiedAmount = text; } } }
};
vm.createContext(amountCopyContext);
vm.runInContext(amountCopyBlock, amountCopyContext, { filename: 'outputs/index.html' });
assert.equal(typeof amountCopyHandler, 'function', '金额复制按钮未注册');
await amountCopyHandler();
assert.equal(copiedAmount, 'PRIMARY', '金额复制夹带了 secondary 文本');
assert.equal(amountCopyStatus.textContent, '已复制', '金额复制成功提示错误');
amountCopyState.values = null;
await amountCopyHandler();
assert.equal(copiedAmount, 'PRIMARY', '无效金额仍执行复制');
assert.equal(amountCopyStatus.textContent, '请先修正金额', '无效金额复制提示错误');
amountCopyState.values = { primary: 'PRIMARY', secondary: 'SECONDARY' };
amountCopyContext.navigator.clipboard.writeText = async () => { throw new Error('blocked'); };
await amountCopyHandler();
assert.equal(amountCopyStatus.textContent, '当前环境不支持自动复制', '复制失败提示错误');
assert.match(html, /小数点使用英文点号；逗号仅按每组三位作千分位/);
assert.doesNotMatch(html, /已转换 · 已取整/);
assert.match(html, /已转换 · 已四舍五入/);

assert.match(html, /window\.addEventListener\('focus', \(\) => renderWorldClocks\(\)\);/);
const pantone = evaluate(['samePantone'], {}, ['samePantone']);
assert.equal(pantone.api.samePantone({ code: '871 C', series: 'MC' }, { code: '871 C', series: 'C' }), false, '潘通选中对象未区分系列');
assert.match(html, /matches\.some\(\(item\) => samePantone\(item, selectedPantone\)\)/);
assert.match(html, /samePantone\(selectedPantone, item\)/);
assert.match(html, /@media \(max-width: 900px\)[\s\S]*\.pantone-results \{ max-height: min\(44vh, 360px\); overflow-y: auto;/);

assert.match(html, /class="tab-lens" aria-hidden="true"/);
assert.match(html, /\.tabs\.has-lens \.tab\.is-preview/);
assert.match(html, /class="tab-lens-shadow" aria-hidden="true"/);
for (const selector of ['.tab-lens', '.tab-lens.is-pressed']) {
  const rule = html.match(new RegExp(`\\n\\s*${selector.replace(/\./g, '\\.')} \\{([^}]*)\\}`))?.[1] || '';
  const shadow = rule.match(/box-shadow:\s*([^;]+);/)?.[1] || '';
  assert.ok(shadow && shadow.split(',').every((part) => part.trim().startsWith('inset')), `${selector} 只能用内阴影；外投影会让折射贴图偏移，应放在 .tab-lens-shadow`);
}
const cornerBlock = html.match(/@supports \(corner-shape: superellipse\(1\.5\)\) \{\n([\s\S]*?)\n    \}\n/)?.[1] || '';
assert.match(cornerBlock, /\*, ::before, ::after \{ corner-shape: superellipse\(1\.5\); \}/);
const roundCorners = new Set((cornerBlock.match(/([^{}]+)\{ corner-shape: round; \}/)?.[1] || '').split(',').map((part) => part.trim()));
const finalRadius = new Map();
for (const [, selectors, radius] of html.slice(html.indexOf('<style'), html.indexOf('</style>')).replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{[^{}]*?border-radius:\s*([^;]+);/g)) {
  for (const selector of selectors.split(',')) finalRadius.set(selector.trim().replace(/\s+/g, ' '), radius.trim());
}
for (const [selector, radius] of finalRadius) {
  if (/^(999px|50%|inherit)$/.test(radius)) assert.ok(roundCorners.has(selector), `${selector} 是胶囊或圆形，须保持 corner-shape: round；导航玻璃胶囊不能继承面板方圆角`);
}
for (const selector of ['.tabs-shell', '.tab', '.tab-lens', '.tab-lens-shadow']) assert.ok(roundCorners.has(selector), `${selector} 须保持胶囊圆端`);
// DESIGN.md: rectangular surfaces and controls use a radius token and the one shared superellipse rule; only pills and circles opt out.
const styleSheet = html.slice(html.indexOf('<style'), html.indexOf('</style>')).replace(/\/\*[\s\S]*?\*\//g, '');
for (const [, value] of styleSheet.matchAll(/border(?:-[a-z]+)*-radius:\s*([^;]+);/g)) {
  assert.match(value.trim(), /^(var\(--radius-[a-z-]+\)|999px|50%|inherit|0)$/, `圆角须使用 --radius-* 令牌（胶囊/圆形除外）：${value}`);
}
for (const selector of roundCorners) assert.match(finalRadius.get(selector) || '', /^(999px|50%|inherit)$/, `${selector} 不是胶囊或圆形，不能退出超椭圆圆角`);
assert.equal((styleSheet.match(/corner-shape\s*:[^;{}]*;/g) || []).length, (cornerBlock.match(/corner-shape\s*:[^;{}]*;/g) || []).length, 'corner-shape 只在共享的 @supports 规则里设置，不在组件里单独覆盖');
const radiusTokens = [...new Set([...html.matchAll(/(--radius-[a-z-]+):/g)].map((match) => match[1]))];
const designDoc = await readFile(join(root, 'DESIGN.md'), 'utf8');
for (const token of radiusTokens) assert.ok(designDoc.includes(token), `DESIGN.md 须说明圆角令牌 ${token}`);
assert.match(designDoc, /corner-shape: superellipse\(1\.5\)/);
assert.match(await readFile(join(root, 'AGENTS.md'), 'utf8'), /\[DESIGN\.md\]\(DESIGN\.md\)/, 'AGENTS.md 须链接 DESIGN.md');
for (const selector of ['.home-ai-chip', '.home-ai-select', '.home-ai-more']) {
  assert.match(finalRadius.get(selector) || '', /^var\(--radius-(button-compact|small)\)$/, `${selector} 须使用紧凑控件的圆角令牌`);
  assert.ok(!roundCorners.has(selector), `${selector} 是矩形控件，须保持超椭圆圆角`);
}
assert.equal((html.match(/<select class="home-ai-select" id="home-ai-(range|sort)"/g) || []).length, 2, 'AI 观察的时间范围和排序选择共用 .home-ai-select');
assert.match(styleSheet, /\.home-ai-chip\[aria-pressed="true"\] \{ border-color: var\(--accent\); background: var\(--accent-soft\); color: var\(--accent-hover\); \}/, 'AI 观察类别按钮的选中态沿用置顶按钮的强调色');
assert.match(styleSheet, /\.home-ai-select:focus \{ border-color: var\(--accent\); box-shadow: var\(--focus-ring\); \}/);
assert.match(styleSheet, /\.home-ai-chip:focus-visible,\s*\.home-ai-more:focus-visible \{ outline: 0; box-shadow: var\(--focus-ring\); \}/, 'AI 观察按钮的键盘焦点使用共享焦点环');
assert.match(html, /\.tab\.is-active \{[\s\S]*?font-weight: 500;/);
assert.match(html, /\[t\.ready, t\.updateCallbackDone, t\.finished\]\.forEach/);
assert.match(html, /handleViewTransitionRejection/);
assert.doesNotMatch(html, /class="tab-en"/);
assert.match(html, /外贸常用工具，一处完成。/);
assert.doesNotMatch(html, /class="welcome-cover-mark"|class="welcome-meta"|class="welcome-actions"/);
assert.match(html, /\.welcome-grid > \.home-ai-card \{ align-self: start; box-shadow: none; \}/);
assert.match(html, /class="memo-calendar-card"[^>]*open/);
assert.ok(html.includes("invoke('read_ai_status')"), 'AI observation must read the worker status separately from feed data');
assert.match(html, /state === 'no_new_items'/);
assert.match(html, /event\.summary_mode === 'extractive-v1'/);
assert.match(html, /home-ai-summary-note/);
const aiFixture = JSON.parse(await readFile(join(root, 'development', 'desktop', 'tests', 'fixtures', 'display-selection.json'), 'utf8'));
const aiCardNames = ['aiFeedPriorityOrder', 'aiFeedText', 'aiFeedPriority', 'aiFeedDate', 'aiFeedWindowDays', 'aiFeedDayNumber', 'aiFeedLocalDay', 'aiFeedEventDay',
  'aiFeedNormalize', 'aiFeedRepeatsTitle', 'aiFeedTypeGroups', 'aiFeedGroup', 'aiFeedCompareKeys', 'aiFeedByPriority', 'aiFeedByNewest', 'aiFeedDisplayEvents', 'aiFeedDayLabel', 'aiFeedEvidenceAfterTitle', 'aiFeedSummaryText', 'aiFeedStatusText', 'aiFeedStatusTitle', 'aiFeedBusy', 'aiFeedFailureSummary', 'aiFeedViewChoices', 'aiFeedReadView'];
const aiCard = evaluate(aiCardNames, {}, ['aiFeedReadView', 'aiFeedGroup', 'aiFeedDisplayEvents', 'aiFeedDayLabel', 'aiFeedSummaryText', 'aiFeedRepeatsTitle', 'aiFeedDate', 'aiFeedStatusText', 'aiFeedStatusTitle', 'aiFeedFailureSummary']);
for (const testCase of aiFixture.cases) {
  assert.deepEqual(
    Array.from(aiCard.api.aiFeedDisplayEvents(aiFixture.events, new Date(testCase.now), testCase.limit), (event) => event.key),
    testCase.expected,
    `首页 AI 观察的选择规则必须与 Python 工作进程一致: ${testCase.now}`
  );
}
for (const testCase of aiFixture.zone_cases) {
  assert.deepEqual(
    Array.from(aiCard.api.aiFeedDisplayEvents(aiFixture.zone_events, new Date(testCase.now), testCase.limit, testCase.tz_offset_minutes), (event) => event.key),
    testCase.expected,
    `首页 AI 观察在固定时区下的日界与窗口边界必须与 Python 一致: ${testCase.name}`
  );
}
const viewNow = new Date('2026-10-28T12:00:00Z');
const viewEvents = [
  { key: 'p0-old', priority: 'P0', event_type: 'release', title: 'Old big launch', published_at: '2026-10-20' },
  { key: 'p3-new', priority: 'P3', event_type: 'pricing', title: 'Price tweak', published_at: '2026-10-28' },
  { key: 'p1-mid', priority: 'P1', event_type: 'security', title: 'Outage', published_at: '2026-10-25' },
  { key: 'p2-ancient', priority: 'P2', event_type: 'research', title: 'Paper', published_at: '2026-09-10' }
];
const viewKeys = (view, limit = Infinity, events = viewEvents) => Array.from(aiCard.api.aiFeedDisplayEvents(events, viewNow, limit, 0, view), (event) => event.key);
assert.deepEqual(viewKeys({}), ['p1-mid', 'p3-new'], '默认视图沿用按优先级的近期窗口');
assert.deepEqual(viewKeys({ days: 7 }), ['p1-mid', 'p3-new'], '时间范围只筛选，不改变默认的优先级排序');
assert.deepEqual(viewKeys({ days: 30 }), ['p0-old', 'p1-mid', 'p3-new'], '30 天范围包含一周前的条目，不含更早的');
assert.deepEqual(viewKeys({ days: 30, group: 'model' }), ['p0-old'], '类别筛选只留下该类条目');
assert.deepEqual(viewKeys({ days: 30, group: 'all' }, 2), ['p0-old', 'p1-mid'], '“全部”不筛选，且仍受条数上限约束');
// Publication dates, discovery times and priorities that disagree, so each sort shows which one it follows.
const sortEvents = [
  { key: 'a', priority: 'P0', event_type: 'release', title: 'Acme 5 launches', published_at: '2026-10-26', discovered_at: '2026-10-28T09:00:00Z' },
  { key: 'b', priority: 'P2', event_type: 'pricing', title: 'Price change', discovered_at: '2026-10-27T10:00:00Z' },
  { key: 'c', priority: 'P2', event_type: 'release', title: 'Mini model', published_at: '2026-10-28T06:00:00Z', discovered_at: '2026-10-28T06:30:00Z' },
  { key: 'd', priority: 'P1', event_type: 'security', title: 'Outage report', published_at: '2026-10-28', discovered_at: '2026-10-28T11:00:00Z' },
  { key: 'e', priority: 'P2', event_type: 'research', title: 'New paper', discovered_at: '2026-10-28T08:00:00Z' },
  { key: 'f', priority: 'P1', event_type: 'release', title: 'Weights out', published_at: '2026-10-27', discovered_at: '2026-10-27T05:00:00Z' },
  { key: 'g', priority: 'P3', event_type: 'release', title: 'Acme 5 launches!', published_at: '2026-10-28', discovered_at: '2026-10-28T10:00:00Z' },
  { key: 'h', priority: 'P2', event_type: 'policy', title: 'Rule one', published_at: '2026-10-25', discovered_at: '2026-10-25T01:00:00Z' },
  { key: 'i', priority: 'P2', event_type: 'policy', title: 'Rule two', published_at: '2026-10-25', discovered_at: '2026-10-25T01:00:00Z' }
];
const byPriority = ['a', 'd', 'f', 'e', 'c', 'b', 'h', 'i'];
const byNewest = ['e', 'c', 'd', 'b', 'f', 'a', 'h', 'i'];
assert.deepEqual(viewKeys({ days: 30 }, Infinity, sortEvents), byPriority, '优先级优先：P0→P3，同级内较新的日期在前');
assert.deepEqual(viewKeys({ days: 30, sort: 'priority' }, Infinity, sortEvents), byPriority, '未选排序与“优先级优先”一致');
assert.deepEqual(viewKeys({ days: 30, sort: 'newest' }, Infinity, sortEvents), byNewest, '最新优先：有发布日期按发布日期（a 虽然最近才发现仍排在后面），没有时按发现时间');
for (const [sort, expected] of [['priority', byPriority], ['newest', byNewest]]) {
  assert.deepEqual(viewKeys({ days: 30, sort }, Infinity, [...sortEvents].reverse()), expected, `${sort} 排序与输入顺序无关，刷新后不会跳动`);
}
assert.deepEqual(viewKeys({ days: 30, sort: 'newest' }, 3, sortEvents), ['e', 'c', 'd'], '收起时的三条取自筛选和排序之后');
assert.deepEqual(viewKeys({ days: 30, sort: 'newest', group: 'model' }, Infinity, sortEvents), ['c', 'f', 'a'], '排序与类别筛选同时生效');
assert.deepEqual(viewKeys({ days: 30, sort: 'priority', group: 'model' }, Infinity, sortEvents), ['a', 'f', 'c']);
assert.deepEqual(viewKeys({ sort: 'newest' }, Infinity, [...sortEvents, { key: 'old-p3', priority: 'P3', title: 'Old minor', published_at: '2026-10-24' }]), byNewest, '最新优先仍受近期窗口约束');
assert.deepEqual(viewKeys({ days: 30, sort: 'newest' }, Infinity, [...sortEvents, { key: 'soon', priority: 'P2', title: 'Retires next week', published_at: '2026-11-04' }])[0], 'soon', '将来生效的日期按今天计');
assert.deepEqual(viewKeys({ days: 30, sort: 'newest' }, 2, [
  { key: 'later', priority: 'P2', title: 'Retires in two weeks', published_at: '2026-11-11' },
  { key: 'sooner', priority: 'P1', title: 'Retires next week', published_at: '2026-11-04' }
]), ['sooner', 'later'], '几条将来日期都按今天计，再按优先级排，不按日期远近');
// UTC+8: discovered at 01:00 local today vs published 04:00 local yesterday by its UTC date; the order follows the day labels.
assert.deepEqual(Array.from(aiCard.api.aiFeedDisplayEvents([
  { key: 'yesterday', priority: 'P2', title: 'Published late UTC', published_at: '2026-10-27T20:00:00Z' },
  { key: 'today', priority: 'P2', title: 'Seen after local midnight', discovered_at: '2026-10-27T17:00:00Z' }
], new Date('2026-10-28T02:00:00Z'), Infinity, 480, { days: 7, sort: 'newest' }), (event) => event.key), ['today', 'yesterday'], '最新优先与“今天/昨天”标签的日期一致');
const shuffledTies = [{ key: 't2', priority: 'P2', title: 'Tie two', published_at: '2026-10-25' }, { key: 't1', priority: 'P2', title: 'Tie one', published_at: '2026-10-25' }];
assert.deepEqual(Array.from(aiCard.api.aiFeedDisplayEvents(shuffledTies, viewNow, Infinity, 0, { days: 30, sort: 'newest' }), (event) => event.key), ['t1', 't2'], '完全相同的条目按事件键固定顺序');
const readView = (raw) => ({ ...aiCard.api.aiFeedReadView(raw) });
assert.deepEqual(readView('{"group":"model","range":"30","expanded":true}'), { group: 'model', range: '30', sort: 'priority', expanded: true }, '没有排序值的旧偏好仍可读取，并使用默认排序');
assert.deepEqual(readView('{"group":"api","range":"7","sort":"newest","expanded":false}'), { group: 'api', range: '7', sort: 'newest', expanded: false });
assert.deepEqual(readView('{"sort":"oldest","group":"nope"}'), { group: 'all', range: 'recent', sort: 'priority', expanded: false });
assert.deepEqual(readView('not json'), { group: 'all', range: 'recent', sort: 'priority', expanded: false });
assert.deepEqual(readView(''), { group: 'all', range: 'recent', sort: 'priority', expanded: false });
assert.match(html, /aiFeedViewKey = 'tradeDesk\.aiWatchView'/, 'AI 观察视图偏好沿用原存储键');
assert.equal(aiCard.api.aiFeedGroup({ event_type: 'content_change' }), 'other');
assert.equal(aiCard.api.aiFeedGroup({ event_type: 'open_weights' }), 'model');
assert.match(html, /id="home-ai-range"/);
assert.match(html, /<select class="home-ai-select" id="home-ai-sort" aria-label="排序方式">\s*<option value="priority" selected>优先级优先<\/option>\s*<option value="newest">最新优先<\/option>/, '排序选择默认“优先级优先”');
assert.match(html, /id="home-ai-more"[^>]*aria-expanded=/);
for (const testCase of aiFixture.summary_cases) {
  assert.equal(aiCard.api.aiFeedRepeatsTitle(testCase.summary, testCase.title), testCase.hidden, `摘要与标题重复的判定必须与 Python 一致: ${testCase.title} / ${testCase.summary}`);
}
assert.equal(aiCard.api.aiFeedSummaryText({ title: 'Introducing Acorn 3', evidence: 'Introducing Acorn 3\nIntroducing Acorn 3, our most capable model, now available in the API.', summary_mode: 'rule-v1', summary: 'Introducing Acorn 3, our most capable model, now available in the API.' }),
  'Introducing Acorn 3, our most capable model, now available in the API.', '以标题开头但内容更多的摘要不应被隐藏');
assert.equal(aiCard.api.aiFeedSummaryText({ title: 'Acorn 3 launches', evidence: 'Acorn 3 launches\nIt reads longer documents than any earlier model.' }),
  'It reads longer documents than any earlier model.', '没有可信摘要时回退到去掉标题的证据');
assert.equal(aiCard.api.aiFeedDate('2026-10-20T10:00:00').toISOString(), '2026-10-20T10:00:00.000Z', '无时区的时间戳按 UTC 解读，与 Python 工作进程一致');
assert.equal(aiCard.api.aiFeedDate('2026-10-20 10:00').toISOString(), '2026-10-20T10:00:00.000Z');
assert.equal(aiCard.api.aiFeedDate('2026-10-20T10:00:00+08:00').toISOString(), '2026-10-20T02:00:00.000Z');
assert.equal(aiCard.api.aiFeedDate('not a date'), null);
assert.doesNotMatch(html, /aiFeedIsToday|aiFeedTodayEvents/, '首页 AI 观察不应只显示发现于当天的条目');
const aiNow = new Date(2026, 9, 26, 12, 0, 0);
const dayLabel = (published, extra = {}) => aiCard.api.aiFeedDayLabel({ published_at: published, ...extra }, aiNow);
assert.equal(dayLabel('2026-10-26T00:00:00+00:00'), '今天');
assert.equal(dayLabel('2026-10-25T00:00:00+00:00'), '昨天');
assert.equal(dayLabel('2026-10-23T00:00:00+00:00'), '3 天前');
assert.equal(dayLabel('2026-10-10T00:00:00+00:00'), '10月10日');
assert.equal(dayLabel('2026-11-15T00:00:00+00:00'), '11月15日生效');
assert.equal(dayLabel('', { discovered_at: new Date(2026, 9, 26, 8).toISOString() }), '今天');
assert.equal(dayLabel(''), '');
const aiEvidence = '先发布了新模型。\nThe endpoint now accepts batches of requests.';
const aiSummary = (extra) => aiCard.api.aiFeedSummaryText({ title: 'Acorn 3 launches', evidence: aiEvidence, ...extra });
assert.equal(aiSummary({ summary: 'The endpoint now accepts batches of requests.', summary_mode: 'rule-v1' }), 'The endpoint now accepts batches of requests.');
assert.equal(aiSummary({ summary: 'The endpoint now accepts batches of requests.', summary_mode: 'extractive-v1' }), 'The endpoint now accepts batches of requests.');
assert.equal(aiSummary({ summary: 'Invented sentence that is not in the evidence.', summary_mode: 'extractive-v1' }), aiEvidence.slice(0, 180), '未通过原文校验的摘要必须回退到证据原文');
assert.equal(aiCard.api.aiFeedSummaryText({ title: 'Acorn 3 launches!', evidence: 'Acorn 3 launches', summary_mode: 'rule-v1', summary: '' }), '', '与标题重复的摘要不应再显示一遍');
const recent = new Date(Date.now() - 3600 * 1000).toISOString();
assert.equal(aiCard.api.aiFeedStatusText({ state: 'partial', failed_sources: 12, pending_sources: 0, new_items: 0, last_success_at: recent }), '已检查 · 12 个来源未响应');
assert.equal(aiCard.api.aiFeedStatusText({ state: 'partial', failed_sources: 2, pending_sources: 1, new_items: 3, last_success_at: recent }), '更新 +3 · 3 个来源未响应');
assert.equal(aiCard.api.aiFeedStatusText({ state: 'partial', failed_sources: 2, new_items: 0, last_success_at: new Date(Date.now() - 3 * 86400000).toISOString() }), '部分失败 · 过期');
assert.equal(aiCard.api.aiFeedStatusText({ state: 'no_new_items', last_success_at: new Date(Date.now() - 25 * 3600000).toISOString() }), '今日暂无新增', '每日一次的更新在到点前不应提前显示“数据过期”');
assert.equal(aiCard.api.aiFeedStatusText({ state: 'no_new_items', last_success_at: new Date(Date.now() - 37 * 3600000).toISOString() }), '数据过期');
assert.equal(aiCard.api.aiFeedStatusText({ state: 'no_new_items', last_success_at: recent }), '今日暂无新增');
const aiTooltip = aiCard.api.aiFeedStatusTitle({ state: 'partial', failed_sources: 12, failure_kinds: { '被拦截': 4, '超时': 8, '其他': 0 }, model_state: 'unavailable', message: '已更新 2 条情报' });
assert.match(aiTooltip, /失败来源：12（被拦截 4、超时 8）/);
assert.match(aiTooltip, /本地模型暂不可用/);
const failureStatus = { state: 'partial', failed_sources: 12, failure_kinds: { '超时': 8, '被拦截': 4 } };
assert.match(aiCard.api.aiFeedFailureSummary(failureStatus), /^12 个来源未响应：超时 8、被拦截 4。可点右上角刷新重试/);
assert.equal(aiCard.api.aiFeedFailureSummary({ ...failureStatus, state: 'updating' }), '12 个来源未响应：超时 8、被拦截 4');
assert.equal(aiCard.api.aiFeedFailureSummary({ state: 'updated', failed_sources: 0 }), '');
assert.match(html, /id="home-ai-refresh"/);
assert.match(html, /home-ai-refresh\[aria-disabled="true"\]/, '刷新按钮忙碌时用 aria-disabled，避免键盘焦点被 disabled 夺走');
assert.doesNotMatch(html, /homeAiRefresh\.disabled/, '刷新按钮不应使用 disabled 属性');
assert.match(html, /next\.innerHTML === homeAiEvents\.innerHTML/, '内容未变化时不应重建 AI 卡片，否则轮询会丢失键盘焦点并重复朗读');
assert.ok(html.includes("invoke('refresh_ai_news')"), '首页 AI 观察的手动刷新必须调用 Rust 命令，而不是由前端自行采集');
assert.match(await readFile(join(root, 'development', 'desktop', 'src-tauri', 'src', 'lib.rs'), 'utf8'), /refresh_ai_news,/);
const aiWatchRust = await readFile(join(root, 'development', 'desktop', 'src-tauri', 'src', 'ai_watch.rs'), 'utf8');
assert.match(aiWatchRust, /app_local_data_dir\(\)/);
assert.match(aiWatchRust, /CUBICAI1/);
assert.match(aiWatchRust, /JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE/);
const aiWatchWorker = await readFile(join(root, 'development', 'desktop', 'ai_radar', 'watch.py'), 'utf8');
assert.match(aiWatchWorker, /suspicious_instruction/);
assert.match(aiWatchWorker, /summary_mode/);
assert.match(aiWatchWorker, /replace_with_retry/, '情报文件必须经带重试的原子替换发布');
assert.match(await readFile(join(root, 'development', 'desktop', 'ai_radar', 'core.py'), 'utf8'), /os\.replace/);
for (const file of ['development/desktop/package.json', 'development/desktop/package-lock.json',
  'development/desktop/src-tauri/tauri.conf.json', 'development/desktop/src-tauri/Cargo.toml',
  'development/desktop/src-tauri/Cargo.lock']) {
  assert.match(await readFile(join(root, file), 'utf8'), /0\.1\.5-dev-0921/, `版本不一致: ${file}`);
}
for (const script of html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)) new vm.Script(script[1], { filename: 'outputs/index.html' });

console.log('PASS: actual calculator, memo, packing, clock, Pantone, CSS and Tab source checks');
