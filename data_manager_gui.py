#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据本地化管理工具 - GUI界面

提供友好的界面管理AI学习数据：
1. 查看数据摘要
2. 导出数据包
3. 导入数据包
4. 创建/恢复备份
"""

import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import json

sys.path.insert(0, str(Path(__file__).parent / "src"))

from bnb_quant_tool.data_localization import DataLocalizationManager


class DataLocalizationGUI:
    """数据本地化管理界面"""
    
    def __init__(self, root):
        self.root = root
        self.root.title("数据本地化管理 - AI学习数据迁移工具")
        self.root.geometry("800x600")
        
        # 设置样式
        style = ttk.Style()
        style.configure('Title.TLabel', font=('Microsoft YaHei', 14, 'bold'))
        style.configure('Info.TLabel', font=('Microsoft YaHei', 10))
        
        # 初始化管理器
        self.manager = DataLocalizationManager()
        
        # 创建界面
        self._create_ui()
        
        # 加载数据
        self._refresh_summary()
    
    def _create_ui(self):
        """创建界面"""
        # 标题
        title_frame = ttk.Frame(self.root, padding="10")
        title_frame.pack(fill=tk.X)
        
        ttk.Label(title_frame, text="📦 数据本地化管理", style='Title.TLabel').pack(side=tk.LEFT)
        
        # 说明
        info_text = """
本工具确保所有AI学习数据保存在工作空间内，支持跨设备迁移。

数据位置：工作空间/data/ 目录
- databases/: 所有数据库文件
- models/: 所有模型文件
- exports/: 导出数据包
- backups/: 自动备份
        """
        ttk.Label(title_frame, text=info_text.strip(), style='Info.TLabel').pack(side=tk.RIGHT)
        
        # 主内容区
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 左侧：数据摘要
        left_frame = ttk.LabelFrame(main_frame, text="数据摘要", padding="10")
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        
        self.summary_text = tk.Text(left_frame, height=20, wrap=tk.WORD, 
                                    font=('Consolas', 10))
        self.summary_text.pack(fill=tk.BOTH, expand=True)
        
        # 右侧：操作按钮
        right_frame = ttk.LabelFrame(main_frame, text="操作", padding="10")
        right_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=(5, 0))
        
        ttk.Label(right_frame, text="📤 数据导出", 
                  font=('Microsoft YaHei', 11, 'bold')).pack(pady=(0, 10))
        
        ttk.Button(right_frame, text="导出全部数据", 
                   command=self._export_all).pack(fill=tk.X, pady=2)
        
        ttk.Separator(right_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
        
        ttk.Label(right_frame, text="📥 数据导入", 
                  font=('Microsoft YaHei', 11, 'bold')).pack(pady=(0, 10))
        
        ttk.Button(right_frame, text="导入数据包", 
                   command=self._import_data).pack(fill=tk.X, pady=2)
        ttk.Button(right_frame, text="导入并合并", 
                   command=self._import_and_merge).pack(fill=tk.X, pady=2)
        
        ttk.Separator(right_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
        
        ttk.Label(right_frame, text="💾 备份管理", 
                  font=('Microsoft YaHei', 11, 'bold')).pack(pady=(0, 10))
        
        ttk.Button(right_frame, text="创建备份", 
                   command=self._create_backup).pack(fill=tk.X, pady=2)
        ttk.Button(right_frame, text="查看备份列表", 
                   command=self._list_backups).pack(fill=tk.X, pady=2)
        ttk.Button(right_frame, text="恢复最近备份", 
                   command=self._restore_latest).pack(fill=tk.X, pady=2)
        
        ttk.Separator(right_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
        
        ttk.Label(right_frame, text="🔧 维护", 
                  font=('Microsoft YaHei', 11, 'bold')).pack(pady=(0, 10))
        
        ttk.Button(right_frame, text="迁移旧数据", 
                   command=self._migrate_old).pack(fill=tk.X, pady=2)
        ttk.Button(right_frame, text="刷新摘要", 
                   command=self._refresh_summary).pack(fill=tk.X, pady=2)
        
        # 底部状态栏
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM)
        
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(status_frame, textvariable=self.status_var, 
                  relief=tk.SUNKEN, padding="5").pack(fill=tk.X)
    
    def _refresh_summary(self):
        """刷新数据摘要"""
        summary = self.manager.get_data_summary()
        
        text = f"""
{'='*60}
数据存储位置
{'='*60}
工作空间: {self.manager.workspace}
数据目录: {self.manager.data_dir}

{'='*60}
总数据大小: {summary['total_size_human']}
{'='*60}

数据库文件:
"""
        
        for name, info in summary.get('databases', {}).items():
            text += f"\n  📄 {name}.db\n"
            text += f"     路径: {info['path']}\n"
            text += f"     大小: {info['size_human']}\n"
            
            tables = info.get('tables', {})
            if tables:
                text += "     表:\n"
                for table, count in tables.items():
                    text += f"       - {table}: {count} 条记录\n"
        
        text += f"\n模型文件:\n"
        for name, info in summary.get('models', {}).items():
            text += f"\n  🧠 {name}\n"
            text += f"     路径: {info['path']}\n"
            text += f"     大小: {info['size_human']}\n"
            text += f"     更新: {info['modified']}\n"
        
        self.summary_text.delete(1.0, tk.END)
        self.summary_text.insert(tk.END, text)
        
        self.status_var.set(f"数据已加载，总大小: {summary['total_size_human']}")
    
    def _export_all(self):
        """导出全部数据"""
        export_name = f"ai_export_{self._get_timestamp()}"
        
        self.status_var.set("正在导出数据...")
        self.root.update()
        
        try:
            export_path = self.manager.export_all(export_name)
            messagebox.showinfo("导出成功", 
                f"数据已导出到:\n{export_path}\n\n"
                f"你可以将此文件复制到新电脑并导入。")
            self.status_var.set(f"导出完成: {export_path.name}")
        except Exception as e:
            messagebox.showerror("导出失败", f"错误: {e}")
            self.status_var.set("导出失败")
    
    def _import_data(self):
        """导入数据（覆盖）"""
        file_path = filedialog.askopenfilename(
            title="选择导入文件",
            filetypes=[("ZIP文件", "*.zip"), ("所有文件", "*.*")]
        )
        
        if not file_path:
            return
        
        if not messagebox.askyesno("确认导入", 
            "导入将覆盖现有数据，是否继续？\n\n建议先创建备份。"):
            return
        
        self.status_var.set("正在导入数据...")
        self.root.update()
        
        try:
            results = self.manager.import_data(file_path, merge=False)
            
            if results.get('checksum_valid'):
                messagebox.showinfo("导入成功", 
                    f"数据导入完成\n\n校验和验证: ✓ 通过")
            else:
                messagebox.showwarning("导入完成", 
                    f"数据导入完成\n\n⚠ 校验和不匹配，数据可能已损坏")
            
            self._refresh_summary()
        except Exception as e:
            messagebox.showerror("导入失败", f"错误: {e}")
            self.status_var.set("导入失败")
    
    def _import_and_merge(self):
        """导入并合并数据"""
        file_path = filedialog.askopenfilename(
            title="选择导入文件",
            filetypes=[("ZIP文件", "*.zip"), ("所有文件", "*.*")]
        )
        
        if not file_path:
            return
        
        self.status_var.set("正在导入并合并数据...")
        self.root.update()
        
        try:
            results = self.manager.import_data(file_path, merge=True)
            
            merged_count = sum(1 for k, v in results.items() 
                             if k.startswith('import_') and v == 'merged')
            copied_count = sum(1 for k, v in results.items() 
                             if k.startswith('import_') and v == 'copied')
            
            messagebox.showinfo("导入成功", 
                f"数据导入完成\n\n"
                f"合并: {merged_count} 个数据库\n"
                f"复制: {copied_count} 个文件")
            
            self._refresh_summary()
        except Exception as e:
            messagebox.showerror("导入失败", f"错误: {e}")
            self.status_var.set("导入失败")
    
    def _create_backup(self):
        """创建备份"""
        backup_name = f"manual_backup_{self._get_timestamp()}"
        
        self.status_var.set("正在创建备份...")
        self.root.update()
        
        try:
            backup_path = self.manager.create_backup(backup_name)
            messagebox.showinfo("备份成功", 
                f"备份已创建:\n{backup_path}")
            self.status_var.set(f"备份完成: {backup_path.name}")
        except Exception as e:
            messagebox.showerror("备份失败", f"错误: {e}")
            self.status_var.set("备份失败")
    
    def _list_backups(self):
        """列出所有备份"""
        backups = self.manager.list_backups()
        
        if not backups:
            messagebox.showinfo("备份列表", "暂无备份")
            return
        
        # 创建新窗口显示备份列表
        backup_win = tk.Toplevel(self.root)
        backup_win.title("备份列表")
        backup_win.geometry("600x400")
        
        tree = ttk.Treeview(backup_win, columns=('name', 'time'), 
                           show='headings', height=15)
        tree.heading('name', text='备份名称')
        tree.heading('time', text='创建时间')
        tree.column('name', width=300)
        tree.column('time', width=200)
        
        for backup in backups:
            tree.insert('', tk.END, values=(
                backup.get('backup_name', 'unknown'),
                backup.get('backup_time', 'unknown')
            ))
        
        tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # 恢复按钮
        def restore_selected():
            selected = tree.selection()
            if not selected:
                return
            
            item = tree.item(selected[0])
            backup_name = item['values'][0]
            
            if messagebox.askyesno("确认恢复", 
                f"确定要恢复备份 '{backup_name}' 吗？\n\n当前数据将被覆盖。"):
                try:
                    self.manager.restore_backup(backup_name)
                    messagebox.showinfo("恢复成功", 
                        f"已恢复备份: {backup_name}")
                    backup_win.destroy()
                    self._refresh_summary()
                except Exception as e:
                    messagebox.showerror("恢复失败", f"错误: {e}")
        
        ttk.Button(backup_win, text="恢复选中备份", 
                   command=restore_selected).pack(pady=10)
    
    def _restore_latest(self):
        """恢复最近备份"""
        backups = self.manager.list_backups()
        
        if not backups:
            messagebox.showwarning("无备份", "没有可用的备份")
            return
        
        latest = backups[0]
        backup_name = latest.get('backup_name')
        
        if messagebox.askyesno("确认恢复", 
            f"确定要恢复最近的备份吗？\n\n"
            f"备份名称: {backup_name}\n"
            f"时间: {latest.get('backup_time', 'unknown')}\n\n"
            f"当前数据将被覆盖。"):
            
            try:
                self.manager.restore_backup(backup_name)
                messagebox.showinfo("恢复成功", 
                    f"已恢复备份: {backup_name}")
                self._refresh_summary()
            except Exception as e:
                messagebox.showerror("恢复失败", f"错误: {e}")
    
    def _migrate_old(self):
        """迁移旧数据"""
        self.status_var.set("正在迁移旧数据...")
        self.root.update()
        
        try:
            results = self.manager.migrate_from_old_locations()
            
            migrated = sum(1 for v in results.values() if v == True)
            skipped = sum(1 for v in results.values() if v == 'skipped')
            failed = sum(1 for v in results.values() if v == False)
            
            messagebox.showinfo("迁移完成", 
                f"数据迁移结果:\n\n"
                f"成功迁移: {migrated} 个文件\n"
                f"已跳过: {skipped} 个文件\n"
                f"失败: {failed} 个文件")
            
            self._refresh_summary()
        except Exception as e:
            messagebox.showerror("迁移失败", f"错误: {e}")
            self.status_var.set("迁移失败")
    
    def _get_timestamp(self):
        """获取时间戳字符串"""
        from datetime import datetime
        return datetime.now().strftime("%Y%m%d_%H%M%S")


def main():
    """主入口"""
    root = tk.Tk()
    app = DataLocalizationGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
