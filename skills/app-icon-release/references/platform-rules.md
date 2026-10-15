# 三平台官方图标规则

规则核对日期：2026-09-16。以下“硬规则”只引用平台/商店官方文档；项目脚本和历史构建证据另列为工具链或项目惯例。页面内容、最低系统版本和商店后台可能继续变化，导出前要按目标版本再次核对。

## 先分清三种资产

### 设计源图

设计源图是用于复用、编辑和派生的母版。它不等于任何一个平台的上传文件。对分层图标，保留正方形未裁切画布、透明前景和完整不透明背景；不要把系统圆角、遮罩、阴影或主题着色烘焙进需要平台处理的层。1024×1024 是本工作流在 HarmonyOS、iOS 和多个商店导出中便于保真的源图基线，但不能把它当作所有平台、所有资源类型的唯一硬尺寸。

### 应用包内资源

包内资源受平台配置、最低 API、编译器和资源限定词控制。检查声明和实际包内资源：例如 `AppScope`/入口 Ability、iOS `AppIcon` asset catalog、Android manifest 与 `mipmap`/`drawable`。构建时发生的重采样、压缩或编码转换需要按目标 SDK 的行为解释。

### 商店素材

商店素材是 AGC、App Store Connect 或 Play Console 接受的独立上传对象。它可能与包内图标由同一母版派生，也可能有不同尺寸、alpha 和编码要求。商店通过系统或商店自己的遮罩/阴影时，上传全方形素材，不要预先裁圆角。

## HarmonyOS / AppGallery

官方配置页将图标分为单层图标和分层图标。分层图标由 `layered-image` JSON 指向 `background` 与 `foreground`：前景通常是带透明区域的 PNG，背景应为纯色且不含透明像素。包内图标使用 PNG；官方上架检查要求图标不能自行裁圆角或添加内间距，显示应清晰，不能有明显模糊、拉伸、压缩或锯齿。

当前应用工程应把 1024×1024 正方形前景/背景作为高分辨率源图基线，并按实际支持的 phone、tablet、PC 等资源限定词检查覆盖情况。官方多设备 FAQ 明确指出，若只在 phone 目录放图标而 `base`/tablet 没有可匹配资源，平板可能显示默认图标。不要把某个工程的固定颜色、文件名或 216 像素导出路径写成所有 HarmonyOS 应用的强制格式。

在配置上同时检查 `AppScope/app.json5` 和 `entry/src/main/module.json5`（以及其中 UIAbility 的 `icon`）。官方配置与开发文档说明了资源合并和优先级：同名资源可能由 entry 覆盖 AppScope；Ability 和应用级配置不一致时，实际桌面图标可能不是 `app.json5` 中看起来的那一个。入口启动页可以使用从两层生成的单层合成图，但这和桌面/设置等场景的分层应用图标是不同引用。

官方最佳实践还说明，DevEco Studio 可从分层图标生成单层图标；文档举例的 phone `xxxldpi/media/icon.png` 为 216×216。这个尺寸属于生成/上传变体，不代表分层源图或所有设备的包内尺寸。AppGallery 视觉素材规范页面（页面更新时间 2026-09-08，本次核对日期 2026-09-16）给出的应用图标上传范围是按设备区分的：

- 手机/平板：上传 1 张正方形图标，216×216 或 1024×1024；PNG ≤3 MB，或 WEBP ≤100 KB；不得使用动图。
- PC/二合一：同为 216×216 或 1024×1024 的正方形 PNG，≤3 MB。
- 手表：同一尺寸范围的正方形图标，但要求背景透明；这个透明背景要求只适用于手表项，不能套用到手机/平板。
- 智慧屏：1024×1024 的 PNG/JPG/JPEG，≤3 MB。
- 元服务手机/平板：216×216 PNG，≤500 KB，并使用元服务生成工具。

这些是 AppGallery 商店上传文件的设备项，不是 HarmonyOS 分层源图、所有包内资源或其他平台的通用规则；提交前仍要按目标设备页签和应用审核指南核对，并确保上传图与包内图标一致。

构建包检查要接受工具链的合法转换：特定版本的 OpenHarmony `developtools_global_resource_tool` 源码显示，资源工具可能将应用/Ability 图层缩放编码为 512×512 PNG。验证时以当前 SDK/工具版本的实际产物为准，检查资源 ID、前后景关系、alpha、像素内容和引用是否正确；不要因为包内是 512×512 就否定 1024×1024 源图。不同 SDK 版本需重新确认该行为。

官方来源：

- [应用图标配置与开发（HarmonyOS）](https://developer.huawei.com/consumer/cn/doc/best-practices/bpta-app-icon-configuration)：分层/单层、应用图标与启动页、资源合并优先级、DevEco 生成单层图标及 AppGallery 图标一致性。
- [应用图标规范（HarmonyOS UX）](https://developer.huawei.com/consumer/cn/doc/doccenter-ux-design/application-icon-0000001953444009)：应用图标设计和上架 UX 规范；页面可能按地区或登录状态动态加载。
- [AppGallery 应用视觉素材规范](https://developer.huawei.com/consumer/cn/doc/app/agc-help-app-visual-asset-spec-0000002277607976)：AGC 当前商店素材格式、尺寸和数量；提交前按目标设备和后台页面复核。
- [HarmonyOS 多设备图标 FAQ](https://developer.huawei.com/consumer/cn/doc/doccenter-dev-faq/multi-device-deployment-faqs-1)：资源限定词和 phone/tablet/base 缺失时的显示问题。
- [OpenHarmony global resource tool `compression_parser.cpp`](https://github.com/openharmony/developtools_global_resource_tool/blob/0e3a1532cf801026c6121c6aa5093daff6064a37/src/compression_parser.cpp#L386) 与 [资源打包流程 `file_manager.cpp`](https://github.com/openharmony/developtools_global_resource_tool/blob/0e3a1532cf801026c6121c6aa5093daff6064a37/src/file_manager.cpp#L92)：用于解释特定工具版本的包内缩放行为，不替代当前 SDK 验证。

## Apple iOS / iPadOS

Apple 当前 HIG 将 iOS/iPadOS/macOS 图标定义为正方形 1024×1024 像素源，系统在显示时应用圆角矩形遮罩。导出的层应保持正方形、未遮罩；预先画圆角会影响系统效果并可能造成锯齿。系统会自动生成设置、通知等位置的小尺寸变体。

Xcode 的 asset catalog 支持用一张 1024×1024 高分辨率图片自动生成 iOS/iPadOS 变体，也可以选择提供单独的尺寸/用途资源。iOS/iPadOS 还支持 Light、Dark、Tinted 外观；Xcode 文档中的 Tinted 槽位要求灰度图。外观变体是额外的可选定制面，不等于基础 App Store 图标，也不意味着每个 target 必须手工填满所有外观槽位。当前 HIG 表格还列出 clear light/clear dark 等平台外观名称；是否可用、由哪个工具生成取决于目标系统和 Xcode 版本。

新项目或需要 Liquid Glass/多层外观时，可读取当前 Icon Composer 文档：用 1024×1024 画布组织层，导出层时不要导出画布遮罩，让系统应用裁切。已有 asset catalog 项目可继续沿用其单图或自定义变体流程，是否迁移由目标 Xcode/产品要求决定。

### 传统单层 App Store marketing 图与透明度

如果工程仍把一张 PNG 放入 asset catalog 的 iOS 1,024pt/`ios-marketing` 槽位，应把它当作已经合成的单层 marketing 图，而不是透明前景层。Apple 当前 Xcode 文档确认这个槽位使用 1024×1024 高分辨率图；Apple 的官方归档技术问答 QA1686 明确说图标可以带 alpha channel，但不能有透明区域。因此对这条传统路径可执行的检查是：PNG 为 1024×1024 正方形，所有像素 alpha 都为 255（没有透明角、透明边或半透明像素）；是否存在 alpha 通道本身不是问题，但透明前景必须先叠到完整不透明背景上再放入该槽位。不要把分层源图的透明 foreground 直接提交为单层 marketing 图。

QA1686 是旧版技术问答，不能覆盖当前 Icon Composer 的所有行为。当前 Apple HIG/Xcode 页面没有发布一个适用于每种 Icon Composer 导出物的统一“必须去掉 alpha 通道”判定；它们明确支持层、foreground 的透明度以及 clear/tinted 等外观。对 Icon Composer/现代外观流程，应保留 `.icon` 或 asset catalog 中的分层结构，在 Icon Composer/Xcode 预览和受影响构建中检查各层关系、外观和系统合成；不要对透明 foreground、clear 或 tinted 源层套用单层 marketing 图的全不透明检查。若另行生成一个扁平 PNG 作为传统 `ios-marketing` 上传/兼容文件，则回到上一段的全像素不透明检查。对官方页面未明确的“分层工程导出后是否会产生单层商店 PNG”细节，记录为 Xcode/目标版本相关，不能臆测成平台硬规则。

App Store Connect 的图标来自 Xcode 构建中的 `AppIcon`/Icon Composer 资源：先在 Xcode 配置，再上传 build。Apple 的官方帮助页说明，发布后修改应用图标需要创建并上传新版本，不能把 App Store Connect 的图标当作与包完全无关的第二张自由设计图。

官方来源：

- [Apple HIG：App icons](https://developer.apple.com/design/human-interface-guidelines/app-icons)：正方形源、系统遮罩、1024×1024 规格、色彩空间和平台/外观表。
- [Xcode：Configuring your app icon](https://developer.apple.com/documentation/xcode/configuring-your-app-icon)：asset catalog、单一 1024 源图自动生成、外观槽位和 iOS 1,024pt App Store 槽位。
- [Xcode：Creating your app icon using Icon Composer](https://developer.apple.com/documentation/xcode/creating-your-app-icon-using-icon-composer)：多层图标、1024×1024 画布、不要导出系统遮罩以及当前 Xcode 适用范围。
- [Apple 技术问答 QA1686（归档）](https://developer.apple.com/library/archive/qa/qa1686/_index.html)：旧版官方图标检查明确允许 alpha channel，但不允许透明区域；仅用于传统扁平 PNG 的透明度核对，并标注其历史范围。
- [Asset Catalog AppIcon 类型（归档）](https://developer.apple.com/library/archive/documentation/Xcode/Reference/xcode_ref-Asset_Catalog_Format/AppIconType.html) 与 [Image Set 类型（归档）](https://developer.apple.com/library/archive/documentation/Xcode/Reference/xcode_ref-Asset_Catalog_Format/ImageSetType.html)：说明 App Store/`ios-marketing` 资源槽位和 1024×1024 资源含义；不把归档格式说明扩大为现代 Icon Composer 的全部规则。
- [App Store Connect：Add an app icon](https://developer.apple.com/help/app-store-connect/manage-app-information/add-an-app-icon)：图标随 Xcode 构建上传，发布后修改需要新版本。
- [App Store Connect：Upload builds](https://developer.apple.com/help/app-store-connect/manage-builds/upload-builds)：构建上传与版本/Bundle ID 关联。

## Android launcher / Google Play

### Adaptive launcher（Android 8.0 / API 26+）

官方 Android Developers 当前文档要求 adaptive icon 的彩色版本提供前景层和背景层，可用 vector 或 bitmap；每层按 108×108 dp 设计。66×66 dp 的内侧安全区用于避免 OEM 形状遮罩裁掉关键内容，四边外侧各 18 dp 留给遮罩和视差/脉动效果。层应有干净边缘，不要在层上再放遮罩或围绕轮廓预烘焙阴影。

如需支持用户主题，提供独立的 monochrome 层。它是包内 launcher 资源，不是 Google Play 上传图；Android 13 起主题图标依赖系统/启动器支持，而当前文档还说明 Android 16 QPR 2 起系统会为没有自有 monochrome 的应用自动主题化。最低版本、启动器能力和是否提供 monochrome 要按产品目标分别判断。

在 manifest 中以 `android:icon` 指向 adaptive 图标；`android:roundIcon` 是可选字段，仅在需要为圆形启动器优化独立外观时提供。不要把 `roundIcon`、adaptive foreground/background 和 Play 商店图混成一种资源。

### Legacy launcher（Android 7.1 / API 25 及以下）

不支持 adaptive icon 的系统使用 legacy launcher bitmap。官方 Android Studio 文档建议将 legacy 图标放在 `res/mipmap-<density>/`，并让 manifest 引用 `mipmap/` 资源。需要兼容旧系统时，从批准的合成图生成 density 变体；这只是旧系统包内资源，不代表 Play 商店图的尺寸。

### Google Play listing icon

Play Console 官方帮助和 Android Developers 的 Play 图标规范把商店图与 launcher 图明确分开。商店图要求：512×512 px、32-bit PNG（含 alpha）、sRGB、最大 1024 KiB。素材应是完整正方形；Google Play 上传后动态处理圆角和阴影（规范页给出的圆角半径相当于图标尺寸的 30%），因此不要预先画圆角或外部 drop shadow。避免在图标中放排名、优惠、Play 类别或其他误导性文字/徽章。

如果将 1024×1024 前景缩进 432×432 bitmap 以代表 108 dp adaptive canvas，这是某些项目按 4× 像素工作时的实现选择；Android 官方硬规则是 dp 图层和安全区，不是 432 px 文件本身。商店图必须再次检查 512 px、alpha、sRGB 和文件大小。

官方来源：

- [Android Developers：Adaptive icons](https://developer.android.com/develop/ui/compose/system/icon_design_adaptive)：108×108 dp 图层、66×66 dp 安全区、18 dp 预留、monochrome、manifest 配置和版本差异。
- [Android API：AdaptiveIconDrawable](https://developer.android.com/reference/android/graphics/drawable/AdaptiveIconDrawable)：adaptive 图层尺寸和系统遮罩的 API 约束。
- [Android Studio：Create app icons](https://developer.android.com/studio/write/create-app-icons)：API 25 及以下 legacy 图标、`mipmap-<density>`、adaptive/legacy 生成和 manifest 引用。
- [Android Developers：Google Play icon design specifications](https://developer.android.com/distribute/google-play/resources/icon-design-specifications)：512×512、32-bit PNG、sRGB、1024 KiB、全方形、Play 动态圆角/阴影。
- [Google Play Console Help：Add preview assets](https://support.google.com/googleplay/android-developer/answer/9866151)：商店图不替代 launcher 图，以及上传时的 PNG、512×512、1024KB 和元数据要求。

## 交付前最小验证

验证范围跟随用户要求，不把完整三端发布回归强加给只导出资产的小任务：

### 只导出/转换资产或商店图

1. 检查 canonical 源图和每个导出图的尺寸、模式/alpha、色彩空间、文件大小、SHA-256 与用途。
2. 检查平台独立的文件规则：HarmonyOS 按当前 AGC 规格，传统 iOS 单层 marketing PNG 按全像素不透明规则，Icon Composer 分层按层结构/外观预览，Android Play 按 512×512/32-bit PNG/sRGB/≤1024KiB。
3. 检查导出清单、命名和复用关系；不要求完整三端构建，也不要求桌面、设置、启动页和商店预览全部运行取证。

### 修改包内配置或资源

1. 除上面的文件检查外，只构建发生变更的平台/target，并从实际包中抽取相关资源，核对资源 ID、尺寸/编码、alpha 和像素。
2. 若 SDK 做了重采样或编码转换，记录 SDK/工具版本和转换后的实际像素，不将转换结果反写为源图规则。入口图标、启动页或关于页变更时，按变更表面做一次针对性预览即可。
3. 不因只改一个平台而要求无关平台构建；只有用户要求多平台发布、或目标行为只能通过运行验证时，才扩展到其他平台/表面。

### 发布或运行验收

在用户明确要求发布/提审或验收运行表面时，再补充构建版本与商店后台处理证据，并在桌面、设置/搜索、启动页和商店预览中检查小尺寸效果。视觉验收不能替代平台审核；普通导出和本地验证也不等于审核通过。
