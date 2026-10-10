import assert from 'node:assert/strict';
import { lstat, mkdir, mkdtemp, readFile, readdir, realpath, rm, writeFile } from 'node:fs/promises';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const rootFiles = ['README.md', 'AGENTS.md', 'PROJECT_CONTEXT.md', 'TASK.md', 'RESULTS.md', '.gitignore'];
const rootDirectories = ['development'];
const optionalRootEntries = new Set(['.git', 'work']);
const developmentDirectories = ['frontend', 'desktop', 'tools'];
const sourceFiles = [
  'development/frontend/index.html',
  'development/frontend/pantone-data.js',
  'development/frontend/trade-desk-logo.svg',
  'development/frontend/trade-desk-logo.png',
  'development/frontend/fonts/TradeDeskNotoSerifSC-Medium.ttf',
  'development/frontend/fonts/OFL-1.1.txt',
  'development/desktop/package.json',
  'development/desktop/package-lock.json',
  'development/desktop/.gitignore',
  'development/desktop/src-tauri/Cargo.toml',
  'development/desktop/src-tauri/Cargo.lock',
  'development/desktop/src-tauri/build.rs',
  'development/desktop/src-tauri/tauri.conf.json',
  'development/desktop/src-tauri/src/lib.rs',
  'development/desktop/src-tauri/src/main.rs',
  'development/desktop/src-tauri/capabilities/default.json',
  'development/desktop/src-tauri/icons/32x32.png',
  'development/desktop/src-tauri/icons/128x128.png',
  'development/desktop/src-tauri/icons/128x128@2x.png',
  'development/desktop/src-tauri/icons/icon.icns',
  'development/desktop/src-tauri/icons/icon.ico',
  'development/tools/directory-check.mjs',
  'development/tools/trade-desk-regression.mjs',
  'development/tools/gray-clay-blue-check.mjs',
  'development/tools/always-on-top-check.mjs'
];

const displayPath = (root, target) => relative(root, target).split(sep).join('/') || '.';
const samePath = (left, right) => process.platform === 'win32'
  ? left.toLowerCase() === right.toLowerCase()
  : left === right;

async function inspectPath(root, target, findings, code) {
  let info;
  try {
    info = await lstat(target);
  } catch (error) {
    if (error.code === 'ENOENT') return { exists: false, safe: false, kind: 'missing' };
    findings.push({ code, path: displayPath(root, target), reason: `无法检查路径：${error.message}` });
    return { exists: false, safe: false, kind: 'unknown' };
  }
  if (info.isSymbolicLink()) {
    findings.push({ code: 'LINK_OR_REPARSE', path: displayPath(root, target), reason: '检测到链接；检查器不会跟随进入。' });
    return { exists: true, safe: false, kind: 'link' };
  }
  const kind = info.isDirectory() ? 'directory' : info.isFile() ? 'file' : 'other';
  if (kind === 'directory') {
    try {
      if (!samePath(await realpath(target), resolve(target))) {
        findings.push({ code: 'LINK_OR_REPARSE', path: displayPath(root, target), reason: '目录解析后指向其他路径；检查器不会进入。' });
        return { exists: true, safe: false, kind };
      }
    } catch (error) {
      findings.push({ code: 'LINK_OR_REPARSE', path: displayPath(root, target), reason: `无法确认目录真实路径：${error.message}` });
      return { exists: true, safe: false, kind };
    }
  }
  return { exists: true, safe: true, kind };
}

async function requireKind(root, relativePath, kind, findings) {
  const info = await inspectPath(root, join(root, relativePath), findings, 'REQUIRED_ENTRY');
  if (!info.exists) {
    findings.push({ code: 'REQUIRED_ENTRY', path: relativePath, reason: `必需${kind === 'directory' ? '目录' : '文件'}缺失。` });
    return false;
  }
  if (info.safe && info.kind !== kind) {
    findings.push({ code: 'REQUIRED_ENTRY', path: relativePath, reason: `类型错误：应为${kind === 'directory' ? '目录' : '普通文件'}，实际为${info.kind}。` });
    return false;
  }
  return info.safe && info.kind === kind;
}

async function checkWork(root, workPath, findings) {
  let entries;
  try { entries = await readdir(workPath); }
  catch (error) {
    findings.push({ code: 'WORK_READ', path: 'work', reason: `无法读取：${error.message}` });
    return;
  }
  for (const name of entries) {
    const taskPath = join(workPath, name);
    const task = await inspectPath(root, taskPath, findings, 'WORK_ENTRY');
    const shown = displayPath(root, taskPath);
    if (!task.safe) continue;
    if (task.kind === 'file') {
      findings.push({ code: 'WORK_SCATTERED_FILE', path: shown, reason: 'work 顶层不允许散落文件；请核实归属并放入登记任务目录。' });
      continue;
    }
    if (task.kind !== 'directory') {
      findings.push({ code: 'WORK_ENTRY_TYPE', path: shown, reason: 'work 顶层只允许普通任务目录。' });
      continue;
    }
    const taskFile = join(taskPath, 'TASK.md');
    const taskInfo = await inspectPath(root, taskFile, findings, 'WORK_TASK');
    if (!taskInfo.exists) {
      findings.push({ code: 'WORK_TASK_MISSING', path: `${shown}/TASK.md`, reason: '缺少项目、用途和状态登记；不能仅凭目录名推断归属。' });
      continue;
    }
    if (!taskInfo.safe || taskInfo.kind !== 'file') continue;
    let contents;
    try { contents = await readFile(taskFile, 'utf8'); }
    catch (error) {
      findings.push({ code: 'WORK_TASK_READ', path: `${shown}/TASK.md`, reason: `无法读取登记：${error.message}` });
      continue;
    }
    const fields = new Map();
    for (const line of contents.split(/\r?\n/)) {
      const match = line.match(/^(项目|用途|状态)：(.*)$/);
      if (!match) continue;
      fields.set(match[1], [...(fields.get(match[1]) ?? []), match[2].trim()]);
    }
    for (const field of ['项目', '用途', '状态']) {
      const values = fields.get(field) ?? [];
      if (values.length !== 1 || !values[0]) findings.push({ code: 'WORK_TASK_FIELD', path: `${shown}/TASK.md`, reason: `“${field}：内容”必须恰好出现一次且非空。` });
    }
    if ((fields.get('项目') ?? []).length === 1 && !['Cubic', 'Trade Desk'].includes(fields.get('项目')[0])) {
      findings.push({ code: 'WORK_TASK_PROJECT', path: `${shown}/TASK.md`, reason: '项目不是 Cubic（原名 Trade Desk）；归属需由规划核实。' });
    }
  }
}

async function inspectLayout(root) {
  const findings = [];
  const absoluteRoot = resolve(root);
  const rootInfo = await inspectPath(absoluteRoot, absoluteRoot, findings, 'PROJECT_ROOT');
  if (!rootInfo.safe || rootInfo.kind !== 'directory') {
    findings.push({ code: 'PROJECT_ROOT', path: '.', reason: '仓库根目录不是可安全检查的普通目录。' });
    return findings;
  }
  let entries;
  try { entries = await readdir(absoluteRoot); }
  catch (error) { return [{ code: 'PROJECT_ROOT', path: '.', reason: `无法读取仓库根目录：${error.message}` }]; }

  const required = new Map([...rootFiles.map((name) => [name, 'file']), ...rootDirectories.map((name) => [name, 'directory'])]);
  const seen = new Set();
  let workSafe = false;
  for (const name of entries) {
    const target = join(absoluteRoot, name);
    const info = await inspectPath(absoluteRoot, target, findings, 'ROOT_ENTRY');
    if (required.has(name)) {
      seen.add(name);
      if (info.safe && info.kind !== required.get(name)) findings.push({ code: 'ROOT_ENTRY_TYPE', path: name, reason: `根目录必需项应为${required.get(name) === 'file' ? '普通文件' : '目录'}。` });
    } else if (optionalRootEntries.has(name)) {
      if (name === 'work') {
        if (info.safe && info.kind === 'directory') workSafe = true;
        else if (info.safe) findings.push({ code: 'ROOT_ENTRY_TYPE', path: name, reason: '可选 work 必须是普通目录。' });
      } else if (info.safe && info.kind !== 'file' && info.kind !== 'directory') {
        findings.push({ code: 'ROOT_ENTRY_TYPE', path: name, reason: '.git 必须是 Git 元数据目录或工作树指针文件。' });
      }
    } else {
      findings.push({ code: 'ROOT_EXTRA', path: displayPath(absoluteRoot, target), reason: '源码仓库根目录包含未列入的文件/目录；本地分发、历史归档和私密材料不应提交。' });
    }
  }
  for (const [name, kind] of required) {
    if (!seen.has(name)) findings.push({ code: 'ROOT_MISSING', path: name, reason: `缺少必需${kind === 'file' ? '项目文件' : '源码目录'}。` });
  }

  const safeDirectory = (path) => requireKind(absoluteRoot, path, 'directory', findings);
  const safeFile = (path) => requireKind(absoluteRoot, path, 'file', findings);
  if (await safeDirectory('development')) {
    const safe = new Map();
    for (const name of ['frontend', 'desktop', 'tools']) safe.set(name, await safeDirectory(`development/${name}`));
    if (safe.get('frontend')) {
      const fonts = await safeDirectory('development/frontend/fonts');
      for (const file of ['index.html', 'pantone-data.js', 'trade-desk-logo.svg', 'trade-desk-logo.png']) await safeFile(`development/frontend/${file}`);
      if (fonts) {
        await safeFile('development/frontend/fonts/TradeDeskNotoSerifSC-Medium.ttf');
        await safeFile('development/frontend/fonts/OFL-1.1.txt');
      }
    }
    if (safe.get('desktop')) {
      await safeFile('development/desktop/package.json');
      await safeFile('development/desktop/package-lock.json');
      await safeFile('development/desktop/.gitignore');
      if (await safeDirectory('development/desktop/src-tauri')) {
        for (const file of ['Cargo.toml', 'Cargo.lock', 'build.rs', 'tauri.conf.json']) await safeFile(`development/desktop/src-tauri/${file}`);
        if (await safeDirectory('development/desktop/src-tauri/src')) {
          await safeFile('development/desktop/src-tauri/src/lib.rs');
          await safeFile('development/desktop/src-tauri/src/main.rs');
        }
        if (await safeDirectory('development/desktop/src-tauri/capabilities')) await safeFile('development/desktop/src-tauri/capabilities/default.json');
        if (await safeDirectory('development/desktop/src-tauri/icons')) {
          for (const file of ['32x32.png', '128x128.png', '128x128@2x.png', 'icon.icns', 'icon.ico']) await safeFile(`development/desktop/src-tauri/icons/${file}`);
        }
      }
    }
    if (safe.get('tools')) {
      for (const file of ['directory-check.mjs', 'trade-desk-regression.mjs', 'gray-clay-blue-check.mjs', 'always-on-top-check.mjs']) await safeFile(`development/tools/${file}`);
    }
  }

  if (workSafe) await checkWork(absoluteRoot, join(absoluteRoot, 'work'), findings);
  return findings;
}

function expectFinding(findings, code, path) {
  assert.ok(findings.some((item) => item.code === code && item.path === path), `expected ${code} at ${path}`);
}

async function selfTest() {
  const root = await mkdtemp(join(tmpdir(), 'trade-desk-source-check-'));
  const directories = [
    '.git', 'development/frontend/fonts', 'development/desktop/src-tauri/src',
    'development/desktop/src-tauri/capabilities', 'development/desktop/src-tauri/icons',
    'development/tools', 'work/sample-task'
  ];
  const files = [
    ...rootFiles,
    'development/frontend/index.html', 'development/frontend/pantone-data.js',
    'development/frontend/trade-desk-logo.svg', 'development/frontend/trade-desk-logo.png',
    'development/frontend/fonts/TradeDeskNotoSerifSC-Medium.ttf', 'development/frontend/fonts/OFL-1.1.txt',
    'development/desktop/package.json', 'development/desktop/package-lock.json', 'development/desktop/.gitignore',
    'development/desktop/src-tauri/Cargo.toml', 'development/desktop/src-tauri/Cargo.lock',
    'development/desktop/src-tauri/build.rs', 'development/desktop/src-tauri/tauri.conf.json',
    'development/desktop/src-tauri/src/lib.rs', 'development/desktop/src-tauri/src/main.rs',
    'development/desktop/src-tauri/capabilities/default.json',
    'development/desktop/src-tauri/icons/32x32.png', 'development/desktop/src-tauri/icons/128x128.png',
    'development/desktop/src-tauri/icons/128x128@2x.png', 'development/desktop/src-tauri/icons/icon.icns',
    'development/desktop/src-tauri/icons/icon.ico',
    'development/tools/directory-check.mjs', 'development/tools/trade-desk-regression.mjs',
    'development/tools/gray-clay-blue-check.mjs', 'development/tools/always-on-top-check.mjs'
  ];
  try {
    for (const directory of directories) await mkdir(join(root, directory), { recursive: true });
    for (const file of files) await writeFile(join(root, file), 'fixture');
    await writeFile(join(root, 'development/desktop/package.json'), JSON.stringify({ scripts: { pretauri: 'node ../tools/directory-check.mjs' } }));
    await writeFile(join(root, 'work/sample-task/TASK.md'), '项目：Cubic\n用途：隔离自检样例\n状态：自检中\n');
    assert.deepEqual(await inspectLayout(root), [], 'valid source tree should pass');

    await writeFile(join(root, '.unexpected'), 'fixture');
    expectFinding(await inspectLayout(root), 'ROOT_EXTRA', '.unexpected');
    await rm(join(root, '.unexpected'));
    await mkdir(join(root, 'outputs'));
    expectFinding(await inspectLayout(root), 'ROOT_EXTRA', 'outputs');
    await rm(join(root, 'outputs'), { recursive: true });

    await rm(join(root, 'development/frontend/index.html'));
    expectFinding(await inspectLayout(root), 'REQUIRED_ENTRY', 'development/frontend/index.html');
    await writeFile(join(root, 'development/frontend/index.html'), 'fixture');

    await mkdir(join(root, 'work/missing-task'));
    expectFinding(await inspectLayout(root), 'WORK_TASK_MISSING', 'work/missing-task/TASK.md');
    await rm(join(root, 'work/missing-task'), { recursive: true });
    await writeFile(join(root, 'work/sample-task/TASK.md'), '项目：Other\n状态：待核实\n');
    const invalid = await inspectLayout(root);
    expectFinding(invalid, 'WORK_TASK_FIELD', 'work/sample-task/TASK.md');
    expectFinding(invalid, 'WORK_TASK_PROJECT', 'work/sample-task/TASK.md');
    console.log('SELF-TEST PASS: source tree, Git metadata, unexpected root/output, missing source, missing/invalid TASK');
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

if (process.argv.length === 3 && process.argv[2] === '--self-test') {
  await selfTest();
} else if (process.argv.length > 2) {
  console.error('Usage: node directory-check.mjs [--self-test]');
  process.exitCode = 2;
} else {
  const findings = await inspectLayout(projectRoot);
  if (!findings.length) console.log(`Source directory check passed: ${projectRoot}`);
  else {
    console.error(`Source directory check found ${findings.length} issue(s):`);
    for (const item of findings) console.error(`- [${item.code}] ${item.path}: ${item.reason}`);
    process.exitCode = 1;
  }
}
