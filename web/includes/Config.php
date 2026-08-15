<?php
declare(strict_types=1);

final class Config
{
    private static ?array $cache = null;

    public static function load(): array
    {
        if (self::$cache !== null) {
            return self::$cache;
        }

        if (!is_file(CONFIG_PATH)) {
            return self::$cache = [];
        }

        if (function_exists('yaml_parse_file')) {
            $parsed = yaml_parse_file(CONFIG_PATH);
            return self::$cache = is_array($parsed) ? $parsed : [];
        }

        return self::$cache = self::parseSimpleYaml(file_get_contents(CONFIG_PATH) ?: '');
    }

    public static function loadRedacted(): array
    {
        $cfg = self::load();
        return self::maskSecrets($cfg);
    }

    public static function reload(): void
    {
        self::$cache = null;
    }

    private static function maskSecrets(array $cfg): array
    {
        foreach (['deepseek', 'qianwen', 'volcengine'] as $prov) {
            if (isset($cfg[$prov]['api_key'])) {
                $key = (string)$cfg[$prov]['api_key'];
                $cfg[$prov]['api_key'] = self::maskKey($key);
            }
        }
        if (isset($cfg['binance'])) {
            $cfg['binance']['api_key'] = '***';
            $cfg['binance']['api_secret'] = '***';
        }
        if (isset($cfg['news']['blockbeats_api_key'])) {
            $key = (string)$cfg['news']['blockbeats_api_key'];
            $cfg['news']['blockbeats_api_key'] = self::maskKey($key);
        }
        if (isset($cfg['web']['api_token'])) {
            $key = (string)$cfg['web']['api_token'];
            $cfg['web']['api_token'] = $key !== '' ? self::maskKey($key) : '';
        }
        if (isset($cfg['onchain']['glassnode_api_key'])) {
            $key = (string)$cfg['onchain']['glassnode_api_key'];
            $cfg['onchain']['glassnode_api_key'] = $key !== '' ? self::maskKey($key) : '';
        }
        return $cfg;
    }

    private static function maskKey(string $key): string
    {
        if (strlen($key) <= 12) {
            return '***';
        }
        return substr($key, 0, 8) . '...' . substr($key, -4);
    }

    /** Web 可远程修改的配置项（不含 API Key） */
    public static function editableSchema(): array
    {
        return [
            'trading' => [
                'confidence_threshold' => ['type' => 'float', 'min' => 0.3, 'max' => 0.95, 'label' => '置信度门槛'],
                'account_balance' => ['type' => 'float', 'min' => 100, 'max' => 10000000, 'label' => '账户余额(模拟)'],
                'symbol' => ['type' => 'string', 'label' => '交易对'],
                'timeframe' => ['type' => 'string', 'label' => '主周期'],
                'risk_per_trade' => ['type' => 'float', 'min' => 0.005, 'max' => 0.05, 'label' => '单笔风险比例'],
            ],
            'auto_run' => [
                'enabled' => ['type' => 'bool', 'label' => '定时分析'],
                'interval_minutes' => ['type' => 'int', 'min' => 5, 'max' => 1440, 'label' => '分析间隔(分钟)'],
            ],
            'signal_scanner' => [
                'enabled' => ['type' => 'bool', 'label' => '信号扫描器'],
                'scan_interval' => ['type' => 'int', 'min' => 30, 'max' => 3600, 'label' => '扫描间隔(秒)'],
                'min_strength' => ['type' => 'float', 'min' => 0.3, 'max' => 1.0, 'label' => '最低触发强度'],
            ],
            'paper_trading' => [
                'auto_follow' => ['type' => 'bool', 'label' => '自动跟单'],
                'relaxed_mode' => ['type' => 'bool', 'label' => '宽松跟单(WAIT时回退)'],
                'poll_interval' => ['type' => 'int', 'min' => 5, 'max' => 120, 'label' => '监控轮询(秒)'],
                'max_position_age_hours' => ['type' => 'int', 'min' => 1, 'max' => 168, 'label' => '硬超时平仓(小时,建议48)'],
                'auto_review_every_n' => ['type' => 'int', 'min' => 1, 'max' => 100, 'label' => '每N笔自动复盘'],
            ],
            'trade_advisor' => [
                'atr_sl_mult' => ['type' => 'float', 'min' => 0.5, 'max' => 5.0, 'label' => 'ATR止损倍数'],
                'atr_tp1_mult' => ['type' => 'float', 'min' => 0.5, 'max' => 10.0, 'label' => 'ATR止盈1倍数'],
                'atr_tp2_mult' => ['type' => 'float', 'min' => 0.5, 'max' => 15.0, 'label' => 'ATR止盈2倍数'],
                'atr_tp3_mult' => ['type' => 'float', 'min' => 0.5, 'max' => 20.0, 'label' => 'ATR止盈3倍数'],
                'news_filter_threshold' => ['type' => 'float', 'min' => 0.0, 'max' => 1.0, 'label' => '新闻过滤阈值'],
                'direction_vote_threshold' => ['type' => 'float', 'min' => 0.05, 'max' => 0.5, 'label' => '方向投票阈值'],
            ],
            'ai_trading' => [
                'follow_ai_direction' => ['type' => 'bool', 'label' => '强制跟随AI方向'],
                'require_gate_pass' => ['type' => 'bool', 'label' => '跟单需通过门控'],
                'use_relaxed_mode' => ['type' => 'bool', 'label' => '宽松跟单(WAIT回退)'],
                'gate_consec_loss_block' => ['type' => 'int', 'min' => 0, 'max' => 20, 'label' => '连亏门控拦截笔数'],
            ],
            'autopilot' => [
                'mode' => ['type' => 'string', 'label' => 'Autopilot模式(off/fullauto/legacy)'],
                'interval_minutes' => ['type' => 'int', 'min' => 5, 'max' => 1440, 'label' => 'Autopilot间隔(分钟)'],
                'open_paper_on_fullauto' => ['type' => 'bool', 'label' => '全自动时模拟开仓'],
            ],
            'intelligence_loop' => [
                'enabled' => ['type' => 'bool', 'label' => '智能闭环(感知→记忆)'],
                'inject_experience_brief' => ['type' => 'bool', 'label' => '注入经验摘要'],
                'inject_council_memory' => ['type' => 'bool', 'label' => '注入议会教训'],
            ],
            'capability_memory' => [
                'reuse_known_situation' => ['type' => 'bool', 'label' => '同局面知识复用'],
                'skip_llm_on_reuse' => ['type' => 'bool', 'label' => '复用时跳过LLM'],
                'skip_council_on_reuse' => ['type' => 'bool', 'label' => '复用时跳过议会'],
                'reuse_trade_require_win' => ['type' => 'bool', 'label' => '开仓复用需历史盈利'],
            ],
            'analysis' => [
                'confidence_threshold' => ['type' => 'float', 'min' => 0.3, 'max' => 0.95, 'label' => '分析置信度门槛'],
            ],
            'deepseek' => [
                'enabled' => ['type' => 'bool', 'label' => 'DeepSeek 开启'],
            ],
            'qianwen' => [
                'enabled' => ['type' => 'bool', 'label' => '通义千问 开启'],
            ],
            'volcengine' => [
                'enabled' => ['type' => 'bool', 'label' => '豆包/火山 开启'],
            ],
            'validation_trading' => [
                'enabled' => ['type' => 'bool', 'label' => '验证开平模式'],
                'probe_open' => ['type' => 'bool', 'label' => '软门控时试探小仓'],
            ],
            'local_growth' => [
                'enabled' => ['type' => 'bool', 'label' => '本地成长教练'],
            ],
        ];
    }

    /**
     * @param array<string, array<string, mixed>> $patch
     * @return array{ok: bool, updated: array<string, array<string, mixed>>, errors: string[]}
     */
    public static function patch(array $patch): array
    {
        $schema = self::editableSchema();
        $updated = [];
        $errors = [];

        if (!is_file(CONFIG_PATH)) {
            return ['ok' => false, 'updated' => [], 'errors' => ['config.yaml 不存在']];
        }

        $raw = file_get_contents(CONFIG_PATH);
        if ($raw === false) {
            return ['ok' => false, 'updated' => [], 'errors' => ['无法读取 config.yaml']];
        }

        foreach ($patch as $section => $values) {
            if (!isset($schema[$section]) || !is_array($values)) {
                $errors[] = "不允许修改的配置段: $section";
                continue;
            }
            foreach ($values as $key => $value) {
                if (!isset($schema[$section][$key])) {
                    $errors[] = "不允许修改: $section.$key";
                    continue;
                }
                $rule = $schema[$section][$key];
                $normalized = self::normalizeValue($value, $rule, "$section.$key", $errors);
                if ($normalized === null && !array_key_exists($key, $updated[$section] ?? [])) {
                    continue;
                }
                if ($normalized === null) {
                    continue;
                }
                $newRaw = self::patchKeyInYaml($raw, $section, $key, $normalized);
                if ($newRaw === null) {
                    $errors[] = "未找到配置项: $section.$key";
                    continue;
                }
                $raw = $newRaw;
                $updated[$section][$key] = $normalized;
            }
        }

        if ($updated === []) {
            return ['ok' => false, 'updated' => [], 'errors' => $errors ?: ['没有有效修改']];
        }

        $backup = CONFIG_PATH . '.bak.' . date('YmdHis');
        copy(CONFIG_PATH, $backup);

        if (file_put_contents(CONFIG_PATH, $raw) === false) {
            return ['ok' => false, 'updated' => [], 'errors' => ['写入 config.yaml 失败']];
        }

        self::$cache = null;
        return ['ok' => true, 'updated' => $updated, 'errors' => $errors, 'backup' => basename($backup)];
    }

    private static function normalizeValue(mixed $value, array $rule, string $label, array &$errors): mixed
    {
        $type = $rule['type'] ?? 'string';
        if ($type === 'bool') {
            if (is_bool($value)) {
                return $value;
            }
            $v = filter_var($value, FILTER_VALIDATE_BOOLEAN, FILTER_NULL_ON_FAILURE);
            if ($v === null) {
                $errors[] = "$label 需要布尔值";
                return null;
            }
            return $v;
        }
        if ($type === 'int') {
            if (!is_numeric($value)) {
                $errors[] = "$label 需要整数";
                return null;
            }
            $n = (int)$value;
            if (isset($rule['min']) && $n < $rule['min']) {
                $errors[] = "$label 不能小于 {$rule['min']}";
                return null;
            }
            if (isset($rule['max']) && $n > $rule['max']) {
                $errors[] = "$label 不能大于 {$rule['max']}";
                return null;
            }
            return $n;
        }
        if ($type === 'float') {
            if (!is_numeric($value)) {
                $errors[] = "$label 需要数字";
                return null;
            }
            $n = (float)$value;
            if (isset($rule['min']) && $n < $rule['min']) {
                $errors[] = "$label 不能小于 {$rule['min']}";
                return null;
            }
            if (isset($rule['max']) && $n > $rule['max']) {
                $errors[] = "$label 不能大于 {$rule['max']}";
                return null;
            }
            return $n;
        }

        $s = trim((string)$value);
        if ($s === '') {
            $errors[] = "$label 不能为空";
            return null;
        }
        return $s;
    }

    private static function patchKeyInYaml(string $yaml, string $section, string $key, mixed $value): ?string
    {
        $lines = preg_split('/\r\n|\r|\n/', $yaml) ?: [];
        $inSection = false;
        $sectionIndent = 0;
        $keyPattern = '/^(\s*)' . preg_quote($key, '/') . ':\s*(.*)$/';

        for ($i = 0, $n = count($lines); $i < $n; $i++) {
            $line = $lines[$i];
            if (preg_match('/^(\s*)' . preg_quote($section, '/') . ':\s*$/', $line, $m)) {
                $inSection = true;
                $sectionIndent = strlen($m[1]);
                continue;
            }
            if ($inSection && preg_match('/^(\S)/', $line) && trim($line) !== '' && !str_starts_with($line, '#')) {
                $inSection = false;
            }
            if (!$inSection) {
                continue;
            }
            if (!preg_match($keyPattern, $line, $m)) {
                continue;
            }
            $indent = $m[1] !== '' ? $m[1] : str_repeat(' ', $sectionIndent + 2);
            $lines[$i] = $indent . $key . ': ' . self::formatYamlValue($value);
            return implode("\n", $lines);
        }

        return null;
    }

    private static function formatYamlValue(mixed $value): string
    {
        if (is_bool($value)) {
            return $value ? 'true' : 'false';
        }
        if (is_int($value)) {
            return (string)$value;
        }
        if (is_float($value)) {
            $s = rtrim(rtrim(sprintf('%.6F', $value), '0'), '.');
            return $s === '' ? '0' : $s;
        }
        return $value;
    }

    /** 无 yaml 扩展时的简易解析（足够读取本项目 config.yaml） */
    private static function parseSimpleYaml(string $text): array
    {
        $lines = preg_split('/\r\n|\r|\n/', $text) ?: [];
        $root = [];
        $stack = [&$root];
        $indents = [0];

        foreach ($lines as $line) {
            if ($line === '' || preg_match('/^\s*#/', $line)) {
                continue;
            }

            if (preg_match('/^(\s*)- (.+)$/', $line, $lm)) {
                $indent = strlen($lm[1]);
                while ($indent < end($indents) && count($stack) > 1) {
                    array_pop($stack);
                    array_pop($indents);
                }
                $parent = &$stack[count($stack) - 1];
                $item = trim($lm[2], " '\"");
                if (!is_array($parent)) {
                    continue;
                }
                $parent[] = is_numeric($item)
                    ? (str_contains($item, '.') ? (float)$item : (int)$item)
                    : $item;
                continue;
            }

            if (!preg_match('/^(\s*)([\w_]+):\s*(.*)$/', $line, $m)) {
                continue;
            }

            $indent = strlen($m[1]);
            $key = $m[2];
            $value = trim($m[3]);

            while ($indent < end($indents) && count($stack) > 1) {
                array_pop($stack);
                array_pop($indents);
            }

            $parent = &$stack[count($stack) - 1];

            if ($value === '') {
                $parent[$key] = [];
                $stack[] = &$parent[$key];
                $indents[] = $indent + 2;
                continue;
            }

            if ($value === 'true') {
                $parent[$key] = true;
            } elseif ($value === 'false') {
                $parent[$key] = false;
            } elseif (is_numeric($value)) {
                $parent[$key] = str_contains($value, '.') ? (float)$value : (int)$value;
            } elseif (preg_match('/^- /', $value)) {
                $parent[$key] = [trim(substr($value, 2))];
            } else {
                $parent[$key] = trim($value, " '\"");
            }
        }

        return $root;
    }
}
