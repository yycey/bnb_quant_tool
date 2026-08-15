"""Mixin: BrainIOMixin"""

from gui._imports import *


class BrainIOMixin:
    _BRAIN_FILES = BRAIN_FILES

    def _project_root(self) -> Path:
        """返回项目根目录 (config.yaml 所在目录)"""
        return PROJECT_ROOT

    def _export_brain_zip(self):
        """打包 ai_learning.db + paper_trading.db + config.yaml 为 zip"""
        try:
            root = self._project_root()
            default_name = f"bnb_brain_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            save_path = filedialog.asksaveasfilename(
                title="导出训练包到...",
                defaultextension=".zip",
                initialfile=default_name,
                filetypes=[("ZIP 压缩包", "*.zip")],
            )
            if not save_path:
                return

            included, missing = [], []
            with zipfile.ZipFile(save_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for fname in self._BRAIN_FILES:
                    fp = root / fname
                    if fp.exists():
                        zf.write(str(fp), arcname=fname)
                        included.append(f"{fname} ({fp.stat().st_size // 1024} KB)")
                    else:
                        missing.append(fname)
                # 附带清单便于识别版本
                manifest = {
                    "exported_at": datetime.now().isoformat(),
                    "source_dir": str(root),
                    "files": included,
                    "missing": missing,
                    "tool": "bnb_quant_tool",
                    "version": "brain_pack_v1",
                }
                zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

            size_kb = os.path.getsize(save_path) // 1024
            msg = f"✅ 导出成功：{save_path}\n包大小：{size_kb} KB\n\n包含文件：\n  - " + "\n  - ".join(included)
            if missing:
                msg += "\n\n⚠ 未找到(跳过)：\n  - " + "\n  - ".join(missing)
            self._brain_status_label.set(f"✅ 已导出: {os.path.basename(save_path)}")
            self.update_status(f"训练包导出成功: {save_path}")
            messagebox.showinfo("导出成功", msg)
        except Exception as e:
            logger.exception("export brain zip failed")
            messagebox.showerror("导出失败", f"打包失败: {e}")

    def _import_brain_zip(self):
        """从 zip 恢复 ai_learning.db / paper_trading.db / config.yaml"""
        try:
            zip_path = filedialog.askopenfilename(
                title="选择要导入的训练包 zip",
                filetypes=[("ZIP 压缩包", "*.zip"), ("所有文件", "*.*")],
            )
            if not zip_path:
                return

            # 预检查
            if not zipfile.is_zipfile(zip_path):
                messagebox.showerror("导入失败", "该文件不是有效的 zip 压缩包")
                return

            with zipfile.ZipFile(zip_path, 'r') as zf:
                names = zf.namelist()
                will_restore = [n for n in self._BRAIN_FILES if n in names]
                if not will_restore:
                    messagebox.showerror("导入失败",
                        f"压缩包中未找到任何可恢复文件（需要：{', '.join(self._BRAIN_FILES)}）")
                    return

            # 二次确认
            confirm = messagebox.askyesno(
                "确认导入",
                f"即将用压缩包中的以下文件覆盖当前项目：\n\n  - " + "\n  - ".join(will_restore) +
                "\n\n原有文件会自动备份为 *.bak_YYYYMMDD_HHMMSS\n确定继续？"
            )
            if not confirm:
                return

            root = self._project_root()
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            backed_up, restored = [], []

            with zipfile.ZipFile(zip_path, 'r') as zf:
                for fname in will_restore:
                    dst = root / fname
                    # 备份原文件
                    if dst.exists():
                        bak = dst.with_suffix(dst.suffix + f".bak_{ts}")
                        try:
                            shutil.copy2(str(dst), str(bak))
                            backed_up.append(bak.name)
                        except Exception as be:
                            logger.warning(f"backup {fname} failed: {be}")
                    # 写入新文件
                    with zf.open(fname) as src, open(str(dst), 'wb') as out:
                        shutil.copyfileobj(src, out)
                    restored.append(fname)

            msg = (
                f"✅ 导入成功！\n\n已恢复文件：\n  - " + "\n  - ".join(restored) +
                (f"\n\n原文件已备份为：\n  - " + "\n  - ".join(backed_up) if backed_up else "") +
                "\n\n⚠ 请关闭并重新启动本工具以加载新的 AI 学习状态和参数。"
            )
            self._brain_status_label.set(f"✅ 已导入 {len(restored)} 个文件，请重启")
            self.update_status(f"训练包导入成功: {len(restored)} 个文件，需重启")
            messagebox.showinfo("导入成功，请重启", msg)
        except Exception as e:
            logger.exception("import brain zip failed")
            messagebox.showerror("导入失败", f"解压/恢复失败: {e}")

