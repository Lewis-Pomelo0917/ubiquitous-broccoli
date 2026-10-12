import assert from 'node:assert/strict';
import { readFile, stat } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../..', import.meta.url));
const html = await readFile(join(root, 'development', 'frontend', 'index.html'), 'utf8');
const svg = await readFile(join(root, 'development', 'frontend', 'trade-desk-logo.svg'), 'utf8');
const svgDark = await readFile(join(root, 'development', 'frontend', 'trade-desk-logo-dark.svg'), 'utf8');
const font = await stat(join(root, 'development', 'frontend', 'fonts', 'TradeDeskNotoSerifSC-Medium.ttf'));

for (const value of ['#F7F5F0', '#FFFEFB', '#F0EFE9', '#2E3336', '#696D67', '#D8D8D0', '#587786', '#496574', '#E8EEF0']) {
  assert.match(html, new RegExp(value, 'i'), `缺少浅色主题值: ${value}`);
}
for (const value of ['#191C1B', '#222725', '#2B312E', '#EEECE5', '#B5B9B2', '#91AFBC', '#283D47']) {
  assert.match(html, new RegExp(value, 'i'), `缺少深色主题值: ${value}`);
}
assert.match(html, /\.welcome-grid > \.home-ai-card \{ align-self: start; box-shadow: none; \}/);
assert.match(html, /\.tabs\.has-lens \.tab\.is-preview/);
assert.match(html, /id="tab-lens-refraction"/);
assert.match(html, /font-family: var\(--font-display\)/);
assert.match(html, /font-family: var\(--font-title\)/);
assert.match(html, /prefers-reduced-motion: reduce/);
const glassBlocks = html.match(/[^{}]*\{[^{}]*backdrop-filter\s*:[^{}]*\}/gi) || [];
assert.ok(glassBlocks.length > 0 && glassBlocks.every((block) => /\.tabs-shell|\.tab-lens/.test(block)), '玻璃效果只能用于顶部菜单外壳和透镜');
const glintBlocks = html.match(/[^{}]*\{[^{}]*(?:linear|radial)-gradient[^{}]*\}/gi) || [];
assert.equal(glintBlocks.length, 1, '仅允许已验收透镜内部的单一光泽层');
assert.match(glintBlocks[0], /\.tab-lens::after\s*\{/);
assert.doesNotMatch(html.replace(glintBlocks[0], ''), /linear-gradient|radial-gradient/i);
assert.doesNotMatch(html, /TD-APPLE|Apple-inspired/i);
for (const mark of [svg, svgDark]) assert.doesNotMatch(mark, /<image\b|<filter\b|data:image\//i, 'Logo 必须为不含位图或模糊滤镜的矢量资源');
assert.match(html, /<img class="brand-logo-dark" src="\.\/trade-desk-logo-dark\.svg"/, '深色主题须显示深色版 Logo');
assert.ok(font.size > 1000, '标题字体文件为空');

console.log('PASS: gray-clay-blue theme, layout, logo and font checks');
