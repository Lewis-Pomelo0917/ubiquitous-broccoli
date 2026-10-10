# Trade Desk 协作规则

## 项目范围

- 本仓库维护 Trade Desk 的原生 HTML/CSS/JavaScript 与 Tauri 2 桌面工具。先阅读本文件、[README.md](README.md)、[PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) 及本次涉及的源码。
- Claude Code 或其他已获授权的仓库协作者可直接查看和修改仓库文件。README 提供背景，不是唯一可读取的资料入口。
- 以当前分支源码和配置为实现依据。发现文档、元数据和代码冲突时，说明差异后再确定，不按日期或历史候选静默选版。
- 只修改任务范围内的文件。不要整库覆盖；保留远端已有的功能、设计和用户补充内容。

## 产品与前端

- Trade Desk 是本地优先的日常计算和备忘工具，不是 ERP、完整订单跟踪系统、单据审核服务或云端 AI 产品。不要把未实现的 Excel 自动化、PO/PI/CI/PL 管理、生产跟踪或客户同步写成现有功能。
- 主要前端为 `development/frontend/index.html`；潘通参考数据在 `development/frontend/pantone-data.js`。Tauri 主工程位于 `development/desktop/`。
- 保留 `.tab`、`.view`、`data-view`、ARIA 与 `activateView` 导航链；修改 DOM 或事件前检查鼠标、键盘及程序化跳转调用。
- 保留现有暖白/灰陶蓝、浅深主题、正式 Logo、字体资源与许可、键盘可达性、焦点可见、减少动画、窄窗布局和明确错误反馈。液态透镜导航须保留拖动预览、松手切页及取消行为。
- 不改变产品规格、重量、CBM、页/张、净重/毛重、单位换算或舍入规则。估算不等于实测、订舱或承运确认。

## 数据与依赖

- 备忘、主题、计算历史和关闭设置使用本机存储。保留兼容迁移、导入校验、保存失败提示、恢复和删除确认；测试使用虚构内容与隔离存储，不读写真实备忘。
- 不新增依赖、服务端、数据库、同步或自动联网采集，除非用户明确授权。
- 不提交 EXE、配套 AI 新闻 JSON、用户备忘、客户资料、历史归档、Logo 原稿、构建缓存、依赖目录或私密配置。遵守根目录 `.gitignore`。

## 检查、构建与交付

从仓库根目录运行适用的现有源码检查：

```sh
node ./development/tools/directory-check.mjs
node ./development/tools/trade-desk-regression.mjs
node ./development/tools/gray-clay-blue-check.mjs
node ./development/tools/always-on-top-check.mjs
```

目录检查通过仅表示源码仓库布局符合规则，不代表功能、视觉或 Windows EXE 已通过验收。报告未实际执行的检查为未验证。

需要构建时，在被忽略的 `work/<任务>/` 中建立 `TASK.md` 与干净前端暂存；使用单独 Tauri 合并配置，`frontendDist` 以 `development/desktop/src-tauri` 为基准指向暂存 `frontend`。不得写个人盘符路径、`file:` 或 `http:` URL。保留 package/npm 与 Cargo 锁文件；不把依赖、EXE、日志、旧页面或用户备忘放入暂存。

源码或浏览器检查不能代替 Windows WebView2、托盘、单实例、系统缩放和关闭生命周期验证。发布或替换正式 EXE 前必须取得用户确认，并单独记录真实验收范围。
