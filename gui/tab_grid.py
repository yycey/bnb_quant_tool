"""Mixin: GridTabMixin"""

from gui._imports import *


class GridTabMixin:
    def _create_grid_tab(self, parent):
        """创建网格策略标签页"""
        # 参数区
        param_frame = ttk.LabelFrame(parent, text="网格参数", padding="10")
        param_frame.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        param_frame.columnconfigure(1, weight=1)
        param_frame.columnconfigure(3, weight=1)

        ttk.Label(param_frame, text="网格类型:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.grid_type_var = tk.StringVar(value='auto')
        ttk.Combobox(param_frame, textvariable=self.grid_type_var,
                       values=['auto', 'symmetrical', 'upward', 'downward'],
                       width=15, state='readonly').grid(row=0, column=1, padx=5, pady=5)

        ttk.Label(param_frame, text="网格数量:").grid(row=0, column=2, sticky=tk.W, pady=5, padx=(20, 0))
        self.grid_count_var = tk.StringVar(value='auto')
        ttk.Entry(param_frame, textvariable=self.grid_count_var, width=10).grid(row=0, column=3, padx=5, pady=5)
        ttk.Label(param_frame, text="(auto=自动计算)").grid(row=0, column=4, sticky=tk.W, pady=5)

        ttk.Label(param_frame, text="总资金(USDT):").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.grid_capital_var = tk.StringVar(value='10000')
        ttk.Entry(param_frame, textvariable=self.grid_capital_var, width=15).grid(row=1, column=1, padx=5, pady=5)

        # 按钮
        btn_frame = ttk.Frame(param_frame)
        btn_frame.grid(row=1, column=2, columnspan=3, sticky=tk.W, pady=5)
        ttk.Button(btn_frame, text="生成网格策略", command=self._generate_grid).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="保存网格计划", command=self._save_grid_plan).pack(side=tk.LEFT, padx=5)

        # 结果显示区
        result_frame = ttk.Frame(parent)
        result_frame.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=5)
        result_frame.columnconfigure(0, weight=1)
        result_frame.rowconfigure(0, weight=1)

        self.grid_text = scrolledtext.ScrolledText(result_frame, wrap=tk.WORD, height=20, font=('Consolas', 9))
        self.grid_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # 状态
        self.grid_status_var = tk.StringVar(value="就绪 | 请先运行分析生成历史记录")
        ttk.Label(parent, textvariable=self.grid_status_var, relief=tk.SUNKEN, anchor=tk.W).grid(row=2, column=0, sticky=(tk.W, tk.E), padx=5, pady=(5, 0))

    def _generate_grid(self):
        """生成网格策略"""
        try:
            grid_type = self.grid_type_var.get()
            grid_count_str = self.grid_count_var.get()
            capital_str = self.grid_capital_var.get()

            grid_count = None if grid_count_str == 'auto' else int(grid_count_str)
            total_capital = float(capital_str)

            self.grid_status_var.set("正在生成网格策略...")
            self.root.update_idletasks()

            # 获取最近分析
            recent = self.grid_strategy.get_recent_analysis(limit=10)
            if not recent:
                self.grid_text.delete(1.0, tk.END)
                self.grid_text.insert(1.0, "错误: 没有找到历史分析记录。\n请先运行几次分析（主界面点击'开始分析'）。")
                self.grid_status_var.set("错误: 无历史分析记录")
                return

            # 计算网格参数
            params = self.grid_strategy.calculate_grid_params(recent, grid_count=grid_count, grid_type=grid_type)
            if 'error' in params:
                self.grid_text.delete(1.0, tk.END)
                self.grid_text.insert(1.0, f"错误: {params['error']}")
                return

            # 生成下单计划
            plan = self.grid_strategy.generate_grid_order_plan(params, total_capital=total_capital)
            self.current_grid_params = params
            self.current_grid_plan = plan

            # 显示报告
            report = self.grid_strategy.format_grid_report(params, plan)
            self.grid_text.delete(1.0, tk.END)
            self.grid_text.insert(1.0, report)

            self.grid_status_var.set(f"网格策略已生成 | {params['grid_type_cn']} {params['grid_count']}格")
        except Exception as e:
            self.grid_text.delete(1.0, tk.END)
            self.grid_text.insert(1.0, f"生成失败: {str(e)}")
            self.grid_status_var.set(f"错误: {str(e)[:50]}")

    def _save_grid_plan(self):
        """保存网格计划到文件"""
        if not hasattr(self, 'current_grid_plan') or not self.current_grid_plan:
            messagebox.showwarning("无数据", "请先生成网格策略")
            return
        filename = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            title="保存网格计划"
        )
        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump({'params': self.current_grid_params, 'plan': self.current_grid_plan}, f, indent=2, ensure_ascii=False)
                messagebox.showinfo("成功", f"网格计划已保存:\n{filename}")
            except Exception as e:
                messagebox.showerror("保存失败", str(e))

