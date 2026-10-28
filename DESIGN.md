# Cubic 设计约定

适用于 `development/frontend/index.html` 的共享样式。颜色、字体与主题见 [README.md](README.md) 和 [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md)；本文只记录必须保持一致的形状规则。`development/tools/trade-desk-regression.mjs` 会检查下列规则。

## 圆角：默认超椭圆

- 带圆角的矩形表面和控件一律写 `border-radius: var(--radius-*)`，不写像素值。
- 形状来自唯一一条共享规则：`@supports (corner-shape: superellipse(1.5))` 块中的 `*, ::before, ::after { corner-shape: superellipse(1.5); }`。同一块把半径令牌放大约 1.7 倍，使超椭圆与直边衔接更平顺、观感略柔于普通圆角。
- 组件里不单独写 `corner-shape`，也不在样式表末尾追加覆盖层；需要调整时改令牌或这条共享规则。

| 令牌 | 普通圆角 / 超椭圆 | 用于 |
|---|---|---|
| `--radius-panel` | 18px / 30px | 主面板、弹窗 |
| `--radius-card` | 12px / 20px | 卡片、结果块（`--radius` 是它的别名） |
| `--radius-control` | 9px / 15px | 输入框与表单控件外壳 |
| `--radius-button` | 10px / 17px | 标准按钮 `.btn` |
| `--radius-button-compact` | 8px / 14px | 紧凑按钮：`.mini-btn`、分页、页头切换、AI 观察的类别按钮与“展开全部” |
| `--radius-small` | 6px / 10px | 下拉选择（如 `.memo-filter-select`、`.home-ai-select`）、提示块、色块、备忘标签 |
| `--radius-tab-shell`、`--radius-tab-inner` | 14px / 10px | 早期导航令牌，当前未使用（导航为胶囊） |

## 例外：胶囊与圆形

- 有意做成胶囊（`999px`）或圆形（`50%`）的元素保留圆端：顶部导航外壳、标签和液态透镜，本机徽标，`.sample-tag` 等小药丸，圆形关闭与回到顶部按钮。它们须列入同一 `@supports` 块的 `corner-shape: round` 列表；超椭圆会把胶囊端压方，透镜折射贴图也按胶囊计算。
- `round` 列表只放胶囊和圆形，不能用来让矩形控件退出超椭圆。
- 同块内另有两处有意的令牌调整：`.memo-tag` 用 `--radius-small` 以区别于旁边的圆形旗标；`.memo-editor-textarea` 收小右下角，让原生缩放手柄留在曲线内。

## 新增控件

- 按类型选上表令牌；尺寸、边框和字号参照同类控件（如 `.always-on-top-toggle`、`.pantone-page-btn`、`.memo-filter-select`）。
- 状态沿用现有写法：悬停用 `--surface-soft` 或 `--btn-hover`；选中（`aria-pressed="true"`）用 `--accent` 边框、`--accent-soft` 底和 `--accent-hover` 字；按下 `translateY(1px)`；键盘焦点 `box-shadow: var(--focus-ring)`（按钮加入共享 `:focus-visible` 列表，下拉选择用 `:focus`）。焦点环与背景随 `corner-shape` 一起成形。

## 兼容与回退

- `corner-shape` 需要 Chromium 139 及以上；Windows 上的 WebView2 常青版随 Edge 更新。不支持时 `@supports` 整块不生效：控件保持普通圆角和上表较小的半径，布局与尺寸不变。
- 源码检查只能证明规则写对了；形状须在支持 `corner-shape` 的浏览器或 WebView2 中截图确认，未确认的环境如实标为未验证。
