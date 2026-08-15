"""Mixin: BacktestTabMixin"""

from gui._imports import *


class BacktestTabMixin:
    def _create_backtest_tab(self, parent):
        """回测标签页：用历史数据验证策略能不能赚钱"""
        ctrl = ttk.LabelFrame(parent, text="回测参数", padding="10")
        ctrl.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)

        ttk.Label(ctrl, text="回测天数:").grid(row=0, column=0, sticky=tk.W, padx=5)
        self.bt_days_var = tk.StringVar(value=str(self.config.get('backtest', {}).get('default_lookback_days', 180)))
        ttk.Spinbox(ctrl, from_=30, to=720, textvariable=self.bt_days_var, width=8).grid(row=0, column=1, padx=5)

        ttk.Label(ctrl, text="初始资金:").grid(row=0, column=2, sticky=tk.W, padx=(15, 5))
        self.bt_balance_var = tk.StringVar(value=str(self.config.get('backtest', {}).get('initial_balance', 5000)))
        ttk.Entry(ctrl, textvariable=self.bt_balance_var, width=10).grid(row=0, column=3, padx=5)

        ttk.Label(ctrl, text="周期:").grid(row=0, column=4, sticky=tk.W, padx=(15, 5))
        self.bt_tf_var = tk.StringVar(value='1h')
        ttk.Combobox(ctrl, textvariable=self.bt_tf_var, values=['15m', '1h', '4h', '1d'],
                     width=8, state='readonly').grid(row=0, column=5, padx=5)

        ttk.Button(ctrl, text="运行回测", command=self._run_backtest).grid(row=0, column=6, padx=10)
        ttk.Button(ctrl, text="导出结果", command=self._save_backtest).grid(row=0, column=7, padx=5)

        # 输出区
        out_frame = ttk.Frame(parent)
        out_frame.grid(row=1, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), padx=5, pady=5)
        out_frame.columnconfigure(0, weight=1)
        out_frame.rowconfigure(0, weight=1)
        self.backtest_text = scrolledtext.ScrolledText(out_frame, wrap=tk.WORD, font=('Consolas', 10))
        self.backtest_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.backtest_text.insert(1.0, "点击“运行回测”开始。建议至少回测 90 天以上才有统计意义。\n")

    def _run_backtest(self):
        try:
            days = int(self.bt_days_var.get())
            balance = float(self.bt_balance_var.get())
            tf = self.bt_tf_var.get()
            symbol = self.symbol_var.get()
        except Exception as e:
            messagebox.showerror("参数错误", str(e))
            return

        self.backtest_text.delete(1.0, tk.END)
        self.backtest_text.insert(1.0, f"正在拉取 {symbol} {tf} {days} 天 K线...\n")
        self.root.update_idletasks()

        def _do():
            try:
                df = self.fetcher.get_historical_klines(symbol=symbol, interval=tf, start_str=f"{days} days ago")
                if df is None or len(df) < 80:
                    raise Exception("数据不足")
                tf_hours = {'15m': 0.25, '1h': 1.0, '4h': 4.0, '1d': 24.0}.get(tf, 1.0)
                eng = BacktestEngine(
                    initial_balance=balance,
                    risk_per_trade=float(self.config.get('trading', {}).get('risk_per_trade', 0.015)),
                    fee_rate=float(self.config.get('backtest', {}).get('fee_rate', 0.0004)),
                    slippage_pct=float(self.config.get('backtest', {}).get('slippage_pct', 0.0005)),
                    atr_sl_mult=float(self.config.get('backtest', {}).get('atr_sl_mult', 1.5)),
                    atr_tp_mult=float(self.config.get('backtest', {}).get('atr_tp_mult', 3.0)),
                    timeframe_hours=tf_hours,
                )
                res = eng.run(df)
                self.last_backtest = res
                report = BacktestEngine.format_report(res, title=f"{symbol} {tf} 回测报告 ({days}天)")
                self.root.after(0, lambda: self._update_backtest_text(report))
            except Exception as e:
                self.root.after(0, lambda: self._update_backtest_text(f"回测失败: {e}"))

        threading.Thread(target=_do, daemon=True).start()

    def _update_backtest_text(self, txt):
        self.backtest_text.delete(1.0, tk.END)
        self.backtest_text.insert(1.0, txt)

    def _save_backtest(self):
        if not getattr(self, 'last_backtest', None):
            messagebox.showwarning("提示", "请先运行一次回测")
            return
        filename = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
            initialfile=f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(BacktestEngine.to_json(self.last_backtest), f, indent=2, ensure_ascii=False, default=str)
                messagebox.showinfo("成功", f"已保存: {filename}")
            except Exception as e:
                messagebox.showerror("失败", str(e))

    def _run_backtest_with_params(self, params: dict, days: int = 30, interval: str = "1h") -> dict:
        """用指定参数跑回测，返回结果字典"""
        from bnb_quant_tool.backtest_engine import BacktestEngine
        symbol = self.config.get('trading', {}).get('default_symbol', 'BNBUSDT')
        bt_cfg = self.config.get('backtest', {}) or {}
        trading_cfg = self.config.get('trading', {}) or {}
        balance = float(bt_cfg.get('initial_balance', trading_cfg.get('account_balance', 5000)))
        # 拉历史 K 线
        limit = days * 24 if interval == "1h" else days * 96
        df = self.fetcher.get_klines(symbol, interval=interval, limit=min(limit, 1000))
        if df is None or len(df) < 100:
            raise ValueError(f"历史数据不足 ({len(df) if df is not None else 0} 根 K 线)")

        engine = BacktestEngine(
            initial_balance=balance,
            risk_per_trade=float(trading_cfg.get('risk_per_trade', 0.015)),
            fee_rate=float(bt_cfg.get('fee_rate', 0.0004)),
            slippage_pct=float(bt_cfg.get('slippage_pct', 0.0005)),
            atr_sl_mult=float(params.get('atr_sl_mult') or bt_cfg.get('atr_sl_mult', 1.5)),
            atr_tp_mult=float(params.get('atr_tp2_mult') or bt_cfg.get('atr_tp_mult', 3.0)),
            timeframe_hours=1.0 if interval == "1h" else 0.25,
        )
        result = engine.run(df)
        return {
            'symbol': symbol,
            'days': days,
            'bars': len(df),
            'total_trades': result.total_trades,
            'win_rate': result.win_rate,
            'total_return_pct': result.total_return_pct,
            'max_drawdown_pct': result.max_drawdown_pct,
            'sharpe_ratio': result.sharpe_ratio,
            'profit_factor': result.profit_factor,
            'avg_hold_hours': result.avg_hold_hours,
        }

    def _backtest_current_params(self):
        """点 '🔬 回测此参数' 后后台跑回测，结果弹窗"""
        params = self._read_param_entries()
        self._param_apply_label.set("⏳ 回测中... 拉取历史数据并计算中")
        self.update_status("回测启动: 请稍候...")

        def _worker():
            try:
                res = self._run_backtest_with_params(params, days=30, interval="1h")
                self.root.after(0, lambda: self._show_backtest_result(res, params))
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda: self._on_backtest_failed(err_msg))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_backtest_failed(self, err_msg: str):
        self._param_apply_label.set("❌ 回测失败")
        messagebox.showerror("回测失败", f"回测报错: {err_msg}")

    def _show_backtest_result(self, res: dict, params: dict):
        """展示回测结果弹窗"""
        self._param_apply_label.set("✅ 回测完成")
        self.update_status(f"回测完成: 胜率 {res['win_rate']*100:.1f}%, 收益 {res['total_return_pct']:+.2f}%")

        msg = (
            f"🔬 回测报告 ({res['symbol']}, {res['days']}天 / {res['bars']}根K线)\n"
            f"{'='*50}\n"
            f"总交易数:    {res['total_trades']}\n"
            f"胜率:        {res['win_rate']*100:.2f}%\n"
            f"总收益:      {res['total_return_pct']:+.2f}%\n"
            f"最大回撤:    {res['max_drawdown_pct']:.2f}%\n"
            f"夏普比率:    {res['sharpe_ratio']:.2f}\n"
            f"盈亏比:      {res['profit_factor']:.2f}\n"
            f"平均持仓:    {res['avg_hold_hours']:.1f} 小时\n"
            f"{'='*50}\n"
            f"📌 使用参数:\n"
            f"  atr_sl_mult  = {params.get('atr_sl_mult')}\n"
            f"  atr_tp2_mult = {params.get('atr_tp2_mult')}\n\n"
            f"如果胜率 > 50% 且总收益 > 0，该参数组合是有效的。\n"
            f"建议点「✅ 应用到配置文件」后重启生效。"
        )
        messagebox.showinfo("回测结果", msg)

