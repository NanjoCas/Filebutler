# 回收文件夹时的系统文件候选名单（待用户审阅）

整理日期：2026-10-22。本文档只是候选名单，**代码尚未修改**。用户确认后，名单会存成数据文件，并修复 `scan_tree` 中"先判断可见、后查名单"的顺序问题。

## 来源（已抓取原文核对）

| 代号 | 来源 | 版本 | 许可证 | 用法 |
| --- | --- | --- | --- | --- |
| G | github/gitignore 的 `Global/macOS.gitignore`、`Windows.gitignore`、`Linux.gitignore` | 提交 `0e5d690153ca`（2026-10-02） | CC0 1.0 | 主要来源 |
| J | sindresorhus/junk 的 `index.js` | 提交 `1c82e3c5603c`（2026-09-18），最新发布版 v4.0.1 | MIT | 交叉核对 |
| N | nextcloud/desktop 的 `sync-exclude.lst` | 提交 `515e2fe8cbd2`（2026-10-21） | GPL-2.0 | 只作参考，不复制文件内容 |
| B | Dropbox 帮助文档"Files not syncing" | 2026-10-22 抓取 | 网页 | 只作参考 |

更正此前凭记忆的说法：
- 原计划参考的 Syncthing 默认忽略列表**没有抓取**，改用 Nextcloud 的列表。Nextcloud 的列表里也包含了 Syncthing 自己的元数据文件。
- 其余说法都已核实：github/gitignore 是 CC0；junk 里确有 `.swp` 和 `npm-debug.log` 这类非系统条目；Dropbox 列出的是 `desktop.ini`、`thumbs.db`、`.ds_store`、`icon\r`、`.dropbox`、`.dropbox.attr`，以及以 `~$`、`.~` 开头的临时文件。

## 挑选标准

能进入 **M 档**（经确认卡片列出后连同文件夹回收）的条目，必须同时满足四条：

1. 由操作系统或文件管理器自动生成；
2. 删除后系统会自动重建，或者根本不需要；
3. 不含用户的文档数据；
4. 会出现在普通文件夹里，而不只是出现在磁盘根目录。

不满足的条目，按风险分到别的档。**任何不确定的条目都往高一档放。**

## 候选：M 档（系统元数据文件）

| 模式（不区分大小写） | 系统 | 来源 | 内容核验 | 大小上限 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `.DS_Store` | macOS Finder | G J N B | 文件头 `00 00 00 01 42 75 64 31`（已用本机样本核对） | 1MB | 文件夹的显示设置 |
| `._*` | macOS AppleDouble | G J N | 文件头 `00 05 16 07`（规范值，本机未找到样本） | 1MB | 资源分支或扩展属性，在非 APFS 磁盘或压缩包中出现 |
| `.localized` | macOS | G | 通常是空文件 | 4KB | 文件夹名称本地化的标记 |
| `Icon\r` | macOS | G J N B | 数据部分通常为空 | 1MB | **自定义文件夹图标**。删除后图标恢复为默认样式。建议在确认卡片上注明 |
| `Thumbs.db`、`ehthumbs.db`、`ehthumbs_vista.db` | Windows | G J N B | OLE 文件头 `D0 CF 11 E0 A1 B1 1A E1` | 50MB | 缩略图缓存 |
| `desktop.ini` | Windows | G J B | 文本文件，含 `[.ShellClassInfo]` 等小节 | 64KB | **文件夹的自定义设置**（图标、显示名等）。建议在确认卡片上注明 |
| `.directory` | Linux KDE Dolphin | G N | 文本文件，含 `[Dolphin]` 或 `[Desktop Entry]` | 64KB | 文件夹的视图设置 |

## 候选：系统生成的文件夹（M 档，但确认卡片逐项列出数量和大小）

| 模式 | 来源 | 说明 |
| --- | --- | --- |
| `__MACOSX/` | G J | 在 macOS 上打包 zip、再在别处解压时产生，里面全是 `._*` 文件。**这是可见的文件夹**，所以要先按名单识别，再判断是否可见 |
| `.AppleDouble/` | G J | 网络共享上的资源分支目录 |
| `@eaDir/` | J | 群晖 NAS 生成的缩略图目录 |

处理这三类文件夹时，还要检查里面是否只有生成的文件。如果混有其他内容，就降为 H 档。

## 新增"忙"档：说明文件正在被使用，拒绝回收并提示关闭程序

| 模式 | 来源 | 说明 |
| --- | --- | --- |
| `~$*` | N B | Office 文档的锁文件 |
| `.~lock.*#` | N | LibreOffice 的锁文件 |
| `*.idlk`、`*.prlock`、`*.dwl`、`*.dwl2`、`*~lock~` | N | InDesign、Premiere、AutoCAD、Affinity 的锁文件 |
| `.fuse_hidden*`、`.nfs*` | G N | 文件已被删除，但仍有程序打开着它 |

## 其他档：不进入 M 档的条目及原因

| 条目 | 来源 | 处理方式 | 原因 |
| --- | --- | --- | --- |
| `.Spotlight-V100`、`.fseventsd`、`.Trashes`、`.TemporaryItems`、`.DocumentRevisions-V100`、`$RECYCLE.BIN`、`System Volume Information`、`.Trash-*` | G J N | **D 档**，拒绝回收 | 磁盘根目录级别的系统目录。其中 `.Trashes`、`.Trash-*` 和 `$RECYCLE.BIN` 里存的是**用户删除的文件**。在普通文件夹里出现它们，说明这里很可能是磁盘根目录 |
| `.com.apple.timemachine.*`、`.PKInstallSandboxManager*`、`Backups.backupdb`、`.MobileBackups` | G | D 档 | Time Machine 和安装程序的数据 |
| `.LSOverride` | G J | H 档，先问 | 记录的是用户自己选的默认打开程序 |
| `.apdisk` | G N | H 档，先问 | 网络共享（AFP）的磁盘信息，一般只出现在共享的根目录 |
| `.*.swp`、`.*.sw?`、`*.kate-swp` | J N | H 档，先问 | 编辑器的交换文件，可能保存着尚未存盘的内容 |
| `*~` | G J N | 当作可见文件处理 | 编辑器的备份，属于用户内容 |
| `.dropbox`、`.dropbox.attr`、`.sync.ffs_db`、`.symform*`、`.stignore` | N B | H 档，先问，并注明"同步软件的配置" | 删除后可能影响同步。`.stfolder`、`.stversions` 是隐藏文件夹，属于 D 档 |
| `*.part`、`*.crdownload`、`*.filepart` | N | 当作可见文件，在确认卡片上注明"下载可能尚未完成" | 未完成的下载，属于用户内容 |
| `*.lnk`、`*.cab`、`*.msi`、`*.stackdump`、`nohup.out`、`npm-debug.log`、`*.textClipping` | G J N | 当作可见文件 | 快捷方式、安装包、日志、剪贴片段，都是用户的文件 |
| Mac OS 9 遗留文件（`Desktop DB`、`TheFindByContentFolder` 等） | G | 当作可见文件 | 极少见；名字可见，按普通文件处理 |
| `Thumbs.db:encryptable` | G | 不收录 | 这是 NTFS 文件系统上的备用数据流名称，在 macOS 上不会出现 |
| `photothumb.db` | N | 不收录 | 只有 Nextcloud 一个来源，用途没能核实 |

## 实现要点（确认后再做）

1. 判断顺序改为：先查结构（D 档）和锁文件（忙档），再查名单（M 档候选），最后判断是否隐藏或可见。这样也就修复了 `Thumbs.db`、`desktop.ini` 之前进不了 M 档的问题。
2. 名单存为 `agent/system_files.json`。每一项包含：模式、所属系统、档位、来源代号、内容核验方式、大小上限和说明，文件开头记录各来源的提交号和许可证。
3. 内容核验或大小检查不通过的文件，降为 H 档。
4. 测试：每一项都要有一个正常样本；同时验证改名冒充、超过大小上限、大小写变体都会被正确处理，`__MACOSX/` 里混入其他文件时要降为 H 档，锁文件要被拒绝并附带提示。

## 待用户决定

1. 自定义文件夹图标 `Icon\r` 和 `desktop.ini` 记录了用户做过的个性化设置。它们是放在 M 档，在确认卡片上注明；还是放在 H 档，先问一次？我建议放在 M 档并注明。
2. 是否新增"忙"档，也就是遇到锁文件时拒绝回收，并提示用户先关闭对应程序。
