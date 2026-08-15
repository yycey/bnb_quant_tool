<?php
declare(strict_types=1);

final class Bridge
{
    private const CLOSE_TIMEOUT_SEC = 45;

    public static function marketSnapshot(string $symbol = 'BNBUSDT'): ?array
    {
        $script = PROJECT_ROOT . DIRECTORY_SEPARATOR . 'web' . DIRECTORY_SEPARATOR
            . 'scripts' . DIRECTORY_SEPARATOR . 'market_snapshot.py';
        if (!is_file($script)) {
            $script = WEB_ROOT . DIRECTORY_SEPARATOR . 'scripts' . DIRECTORY_SEPARATOR . 'market_snapshot.py';
        }
        if (!is_file($script)) {
            return null;
        }

        $python = self::pythonBinary();
        if ($python === '') {
            return null;
        }

        $output = self::runWithTimeout([$python, $script, $symbol], 20);
        if ($output === null || trim($output) === '') {
            return null;
        }

        $data = self::decodeJsonOutput($output);
        return is_array($data) ? $data : null;
    }

    public static function closePosition(int $pid, float $price = 0.0): ?array
    {
        $script = PROJECT_ROOT . DIRECTORY_SEPARATOR . 'web' . DIRECTORY_SEPARATOR
            . 'scripts' . DIRECTORY_SEPARATOR . 'close_position.py';
        if (!is_file($script)) {
            $script = WEB_ROOT . DIRECTORY_SEPARATOR . 'scripts' . DIRECTORY_SEPARATOR . 'close_position.py';
        }
        if (!is_file($script)) {
            return null;
        }

        $python = self::pythonBinary();
        if ($python === '') {
            return null;
        }

        $args = [$python, $script, (string)$pid];
        if ($price > 0) {
            $args[] = (string)$price;
        }

        $output = self::runWithTimeout($args, self::CLOSE_TIMEOUT_SEC);
        if ($output === null || trim($output) === '') {
            return null;
        }

        $data = self::decodeJsonOutput($output);
        return is_array($data) ? $data : null;
    }

    public static function maintenance(string $action, array $extraArgs = []): ?array
    {
        $script = PROJECT_ROOT . DIRECTORY_SEPARATOR . 'web' . DIRECTORY_SEPARATOR
            . 'scripts' . DIRECTORY_SEPARATOR . 'maintenance.py';
        if (!is_file($script)) {
            $script = WEB_ROOT . DIRECTORY_SEPARATOR . 'scripts' . DIRECTORY_SEPARATOR . 'maintenance.py';
        }
        if (!is_file($script)) {
            return null;
        }

        $python = self::pythonBinary();
        if ($python === '') {
            return null;
        }

        $args = array_merge([$python, $script, $action], $extraArgs);
        $timeout = in_array($action, ['git_pull', 'apply_zip', 'optimize'], true) ? 180 : 90;
        $output = self::runWithTimeout($args, $timeout);
        if ($output === null || trim($output) === '') {
            return null;
        }

        $data = self::decodeJsonOutput($output);
        return is_array($data) ? $data : ['ok' => false, 'error' => 'Invalid JSON from maintenance script', 'raw' => substr($output, 0, 500)];
    }

    /** 触发一轮无 GUI AI 分析（HeadlessAnalysisRunner） */
    public static function runAnalysis(bool $openPaper = true): ?array
    {
        $script = PROJECT_ROOT . DIRECTORY_SEPARATOR . 'web' . DIRECTORY_SEPARATOR
            . 'scripts' . DIRECTORY_SEPARATOR . 'run_analysis.py';
        if (!is_file($script)) {
            $script = WEB_ROOT . DIRECTORY_SEPARATOR . 'scripts' . DIRECTORY_SEPARATOR . 'run_analysis.py';
        }
        if (!is_file($script)) {
            return null;
        }

        $python = self::pythonBinary();
        if ($python === '') {
            return null;
        }

        $args = [$python, $script];
        $args[] = $openPaper ? '--open-paper' : '--no-open-paper';

        $output = self::runWithTimeout($args, 300);
        if ($output === null || trim($output) === '') {
            return null;
        }

        $data = self::decodeJsonOutput($output);
        return is_array($data) ? $data : ['ok' => false, 'error' => 'Invalid JSON from run_analysis', 'raw' => substr($output, 0, 500)];
    }

    /** 学习仪表盘快照（胜率优化 + 亏损模式） */
    public static function learningSnapshot(): ?array
    {
        $script = PROJECT_ROOT . DIRECTORY_SEPARATOR . 'web' . DIRECTORY_SEPARATOR
            . 'scripts' . DIRECTORY_SEPARATOR . 'learning_snapshot.py';
        if (!is_file($script)) {
            $script = WEB_ROOT . DIRECTORY_SEPARATOR . 'scripts' . DIRECTORY_SEPARATOR . 'learning_snapshot.py';
        }
        if (!is_file($script)) {
            return null;
        }

        $python = self::pythonBinary();
        if ($python === '') {
            return null;
        }

        $output = self::runWithTimeout([$python, $script], 45);
        if ($output === null || trim($output) === '') {
            return null;
        }

        $data = self::decodeJsonOutput($output);
        return is_array($data) ? $data : null;
    }

    /** 调用 Python 修复损坏的 SQLite 库（与 GUI init_workspace 共用逻辑） */
    public static function repairDatabases(): ?array
    {
        $script = PROJECT_ROOT . DIRECTORY_SEPARATOR . 'web' . DIRECTORY_SEPARATOR
            . 'scripts' . DIRECTORY_SEPARATOR . 'repair_databases.py';
        if (!is_file($script)) {
            $script = WEB_ROOT . DIRECTORY_SEPARATOR . 'scripts' . DIRECTORY_SEPARATOR . 'repair_databases.py';
        }
        if (!is_file($script)) {
            return null;
        }

        $python = self::pythonBinary();
        if ($python === '') {
            return null;
        }

        $output = self::runWithTimeout([$python, $script], 60);
        if ($output === null || trim($output) === '') {
            return null;
        }

        $data = self::decodeJsonOutput($output);
        return is_array($data) ? $data : null;
    }

    /** PHP 侧基础健康检查（Python 不可用时回退） */
    public static function maintenanceFallbackHealth(): array
    {
        $checks = [];
        $checks[] = [
            'id' => 'config',
            'ok' => is_file(CONFIG_PATH),
            'level' => is_file(CONFIG_PATH) ? 'ok' : 'error',
            'message' => is_file(CONFIG_PATH) ? 'config.yaml 存在' : 'config.yaml 缺失',
            'fixable' => false,
        ];
        $checks[] = [
            'id' => 'data_dir',
            'ok' => is_dir(DATA_DIR),
            'level' => is_dir(DATA_DIR) ? 'ok' : 'error',
            'message' => is_dir(DATA_DIR) ? 'data/ 目录存在' : 'data/ 目录缺失',
            'fixable' => false,
        ];
        foreach (['ai_learning.db', 'paper_trading.db'] as $db) {
            $path = DATA_DIR . DIRECTORY_SEPARATOR . $db;
            $checks[] = [
                'id' => $db,
                'ok' => is_file($path),
                'level' => is_file($path) ? 'ok' : 'error',
                'message' => is_file($path) ? "$db 存在" : "$db 缺失",
                'fixable' => false,
            ];
        }
        $failed = array_filter($checks, static fn($c) => !$c['ok']);
        return [
            'ok' => count($failed) === 0,
            'checks' => $checks,
            'summary' => ['total' => count($checks), 'failed' => count($failed), 'warnings' => 0],
            'fixable' => false,
            'fallback' => true,
            'hint' => 'Python 不可用，仅基础检查。请在 config.yaml 设置 web.python_path',
        ];
    }

    /**
     * @param list<string> $args
     */
    public static function runWithTimeout(array $args, int $timeoutSec = 30): ?string
    {
        if (!function_exists('proc_open')) {
            if (!shell_available()) {
                return null;
            }
            $cmd = implode(' ', array_map('escapeshellarg', $args));
            return shell_exec($cmd . ' 2>&1');
        }

        $descriptors = [
            0 => ['pipe', 'r'],
            1 => ['pipe', 'w'],
            2 => ['pipe', 'w'],
        ];

        $cmd = implode(' ', array_map('escapeshellarg', $args));
        $proc = proc_open($cmd, $descriptors, $pipes);
        if (!is_resource($proc)) {
            return null;
        }

        fclose($pipes[0]);
        stream_set_blocking($pipes[1], false);
        stream_set_blocking($pipes[2], false);

        $stdout = '';
        $stderr = '';
        $start = time();

        while (true) {
            $read = [$pipes[1], $pipes[2]];
            $write = null;
            $except = null;
            $remaining = max(1, $timeoutSec - (time() - $start));
            $n = @stream_select($read, $write, $except, min(2, $remaining));

            if ($n !== false && $n > 0) {
                foreach ($read as $stream) {
                    $chunk = stream_get_contents($stream);
                    if ($stream === $pipes[1]) {
                        $stdout .= $chunk;
                    } else {
                        $stderr .= $chunk;
                    }
                }
            }

            $status = proc_get_status($proc);
            if (!$status['running']) {
                $stdout .= stream_get_contents($pipes[1]);
                $stderr .= stream_get_contents($pipes[2]);
                break;
            }

            if ((time() - $start) >= $timeoutSec) {
                proc_terminate($proc);
                proc_close($proc);
                return null;
            }
        }

        fclose($pipes[1]);
        fclose($pipes[2]);
        proc_close($proc);

        $out = trim($stdout);
        if ($out === '' && trim($stderr) !== '') {
            return trim($stderr);
        }
        return $out !== '' ? $out : null;
    }

    public static function pythonBinary(): string
    {
        $cfg = Config::load();
        $configured = trim((string)($cfg['web']['python_path'] ?? ''));
        if ($configured !== '' && is_file($configured)) {
            return $configured;
        }

        foreach (self::commonPythonPaths() as $path) {
            if (is_file($path)) {
                return $path;
            }
        }

        if (!shell_available()) {
            return '';
        }

        foreach (['python', 'python3', 'py'] as $bin) {
            $found = trim((string)shell_exec(
                strtoupper(substr(PHP_OS, 0, 3)) === 'WIN'
                    ? "where $bin 2>nul"
                    : "command -v $bin 2>/dev/null"
            ));
            if ($found !== '') {
                $first = preg_split('/\r\n|\r|\n/', $found)[0] ?? $bin;
                return $first ?: $bin;
            }
        }
        return '';
    }

    public static function pythonAvailable(): bool
    {
        $bin = self::pythonBinary();
        if ($bin === '') {
            return false;
        }
        if (!shell_available()) {
            return is_file($bin);
        }
        $ver = trim((string)shell_exec(escapeshellarg($bin) . ' --version 2>&1'));
        return stripos($ver, 'python') !== false;
    }

    /** 从脚本 stdout 解析 JSON（兼容日志污染：取最后一个 JSON 对象行） */
    private static function decodeJsonOutput(string $output): ?array
    {
        $trimmed = trim($output);
        if ($trimmed === '') {
            return null;
        }

        $data = json_decode($trimmed, true);
        if (is_array($data)) {
            return $data;
        }

        $lines = preg_split('/\r\n|\r|\n/', $trimmed) ?: [];
        for ($i = count($lines) - 1; $i >= 0; $i--) {
            $line = trim($lines[$i]);
            if ($line === '' || $line[0] !== '{') {
                continue;
            }
            $data = json_decode($line, true);
            if (is_array($data)) {
                return $data;
            }
        }

        return null;
    }

    /** @return list<string> */
    private static function commonPythonPaths(): array
    {
        $localApp = getenv('LOCALAPPDATA') ?: '';
        $paths = [
            'C:\\Python314\\python.exe',
            'C:\\Python313\\python.exe',
            'C:\\Python312\\python.exe',
            'C:\\Python311\\python.exe',
            'C:\\Python310\\python.exe',
            'C:\\Program Files\\Python314\\python.exe',
            'C:\\Program Files\\Python313\\python.exe',
            'C:\\Program Files\\Python312\\python.exe',
            'C:\\Program Files\\Python311\\python.exe',
            'C:\\Program Files\\Python310\\python.exe',
        ];
        if ($localApp !== '') {
            foreach (['Python314', 'Python313', 'Python312', 'Python311'] as $ver) {
                $paths[] = $localApp . '\\Programs\\Python\\' . $ver . '\\python.exe';
            }
            // py launcher: py -3
            $paths[] = $localApp . '\\Programs\\Python\\Launcher\\py.exe';
        }
        return array_merge($paths, [
            'C:\\Python39\\python.exe',
            '/usr/bin/python3',
            '/usr/local/bin/python3',
        ]);
    }
}
