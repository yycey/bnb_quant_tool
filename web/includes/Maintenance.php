<?php
declare(strict_types=1);

/**
 * Web 维护 — PHP 原生实现（备份/优化/修复/健康检查/热更新）
 * 不依赖 Python，适配 PhpStudy / 宝塔 shell_exec 禁用环境
 */
final class Maintenance
{
    private const REQUIRED_DBS = ['ai_learning.db', 'paper_trading.db'];

    private const OPTIONAL_DBS = ['counterfactual.db', 'pattern_memory.db'];

    private const UPDATE_ALLOW_PREFIXES = [
        'src/',
        'web/public/',
        'web/includes/',
        'web/scripts/',
        'gui.py',
        'main.py',
        'paper_watcher.py',
        'requirements.txt',
        '启动.bat',
        '启动web.bat',
        '启动监控.bat',
    ];

    private const UPDATE_PROTECTED = [
        'data/',
        'config.yaml',
        'deploy.local.php',
        '.env',
        '.git/',
    ];

    public static function backup(string $label = 'web'): array
    {
        $label = preg_replace('/[^a-zA-Z0-9_-]/', '', $label);
        if ($label === '') {
            $label = 'web';
        }

        $backupsDir = DATA_DIR . DIRECTORY_SEPARATOR . 'backups';
        if (!is_dir($backupsDir) && !@mkdir($backupsDir, 0755, true) && !is_dir($backupsDir)) {
            return ['ok' => false, 'error' => '无法创建 data/backups 目录，请检查目录权限'];
        }

        if (!class_exists('ZipArchive')) {
            return ['ok' => false, 'error' => 'PHP ZipArchive 扩展未启用，请在 php.ini 中开启 ext-zip'];
        }

        $files = self::collectBackupFiles();
        if ($files === []) {
            return ['ok' => false, 'error' => '没有可备份的文件（需要 config.yaml 或 data/*.db）'];
        }

        $name = 'backup_' . $label . '_' . date('Ymd_His');
        $zipPath = $backupsDir . DIRECTORY_SEPARATOR . $name . '.zip';

        $zip = new ZipArchive();
        $opened = $zip->open($zipPath, ZipArchive::CREATE | ZipArchive::OVERWRITE);
        if ($opened !== true) {
            return ['ok' => false, 'error' => '无法创建备份 zip（错误码 ' . $opened . '）'];
        }

        $arcNames = [];
        foreach ($files as [$src, $arc]) {
            if (!$zip->addFile($src, $arc)) {
                $zip->close();
                @unlink($zipPath);
                return ['ok' => false, 'error' => '打包失败: ' . $arc];
            }
            $arcNames[] = $arc;
        }

        $meta = [
            'name' => $name,
            'created_at' => date('c'),
            'project_root' => PROJECT_ROOT,
            'files' => $arcNames,
            'engine' => 'php',
        ];
        $zip->addFromString(
            'metadata.json',
            json_encode($meta, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT) ?: '{}'
        );
        $zip->close();

        if (!is_file($zipPath)) {
            return ['ok' => false, 'error' => '备份文件未生成'];
        }

        return [
            'ok' => true,
            'backup_path' => $zipPath,
            'backup_name' => basename($zipPath),
            'size_bytes' => filesize($zipPath) ?: 0,
            'files' => $arcNames,
            'engine' => 'php',
        ];
    }

    public static function listBackups(int $limit = 10): array
    {
        $limit = max(1, min(30, $limit));
        $backupsDir = DATA_DIR . DIRECTORY_SEPARATOR . 'backups';
        $items = [];

        if (is_dir($backupsDir)) {
            $pattern = $backupsDir . DIRECTORY_SEPARATOR . '*.zip';
            $files = glob($pattern) ?: [];
            rsort($files, SORT_STRING);
            foreach (array_slice($files, 0, $limit) as $path) {
                $items[] = [
                    'name' => basename($path),
                    'path' => $path,
                    'size_bytes' => filesize($path) ?: 0,
                    'modified' => date('Y-m-d\TH:i:s', (int)filemtime($path)),
                ];
            }
        }

        return ['ok' => true, 'backups' => $items];
    }

    public static function optimize(): array
    {
        $hb = DATA_DIR . DIRECTORY_SEPARATOR . 'watcher.heartbeat';
        if (is_file($hb)) {
            $age = time() - (int)filemtime($hb);
            if ($age >= 0 && $age <= 120) {
                return [
                    'ok' => false,
                    'error' => "模拟盘监控运行中（心跳 {$age}s 前），请先停止监控再 VACUUM，以免锁库导致止盈/平仓失败",
                    'actions' => [],
                    'blocked_by_watcher' => true,
                ];
            }
        }

        $actions = [];
        foreach (array_merge(self::REQUIRED_DBS, self::OPTIONAL_DBS) as $dbName) {
            $path = Database::resolvePath($dbName) ?? (DATA_DIR . DIRECTORY_SEPARATOR . $dbName);
            if (!is_file($path)) {
                continue;
            }
            try {
                $pdo = new PDO('sqlite:' . $path, null, null, [
                    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
                    PDO::ATTR_TIMEOUT => 30,
                ]);
                $pdo->exec('PRAGMA busy_timeout=30000');
                // 在线只做轻量 checkpoint；VACUUM 需独占锁，已在上方按心跳拦截
                $pdo->exec('PRAGMA wal_checkpoint(TRUNCATE)');
                $pdo->exec('VACUUM');
                $pdo = null;
                $sizeMb = round(filesize($path) / 1024 / 1024, 2);
                $actions[] = "{$dbName} 已优化 ({$sizeMb} MB)";
            } catch (Throwable $e) {
                $actions[] = "{$dbName} 优化失败: " . $e->getMessage();
            }
        }

        if ($actions === []) {
            return ['ok' => false, 'error' => '没有可优化的数据库文件', 'actions' => []];
        }

        return ['ok' => true, 'actions' => $actions, 'engine' => 'php'];
    }

    public static function healthCheck(): array
    {
        $checks = [];

        $checks[] = self::checkPath('config.yaml', CONFIG_PATH, true);
        $checks[] = self::checkPath('data 目录', DATA_DIR, true, true);

        foreach (self::REQUIRED_DBS as $db) {
            $path = Database::resolvePath($db);
            $checks[] = [
                'id' => $db,
                'ok' => $path !== null && is_file($path),
                'level' => ($path !== null && is_file($path)) ? 'ok' : 'error',
                'message' => ($path !== null && is_file($path))
                    ? "{$db} 存在: {$path}"
                    : "{$db} 缺失",
                'fixable' => false,
            ];
        }

        foreach (self::OPTIONAL_DBS as $db) {
            $path = Database::resolvePath($db);
            $exists = $path !== null && is_file($path);
            $checks[] = [
                'id' => $db,
                'ok' => true,
                'level' => $exists ? 'ok' : 'warn',
                'message' => $exists ? "{$db} 存在" : "{$db} 缺失（可选）",
                'fixable' => false,
            ];
        }

        $checks[] = self::checkDbSchema();
        $checks[] = self::checkWebFiles();
        $checks[] = self::checkOrphanDbs();
        $checks[] = self::checkZipExtension();
        $checks[] = self::checkWatcherHeartbeat();

        $failed = array_filter($checks, static fn($c) => !($c['ok'] ?? false));
        $warn = array_filter($checks, static fn($c) => ($c['ok'] ?? false) && ($c['level'] ?? '') === 'warn');

        return [
            'ok' => count($failed) === 0,
            'checks' => $checks,
            'summary' => [
                'total' => count($checks),
                'failed' => count($failed),
                'warnings' => count($warn),
            ],
            'fixable' => (bool)array_filter($checks, static fn($c) => !empty($c['fixable'])),
            'engine' => 'php',
        ];
    }

    public static function autoFix(): array
    {
        $actions = array_merge(
            self::migrateRootDbs(),
            self::ensureDirectories()
        );

        if (class_exists('Bridge')) {
            $repair = Bridge::repairDatabases();
            if (is_array($repair)) {
                if (!empty($repair['ok'])) {
                    $actions[] = 'SQLite 数据库健康检查/修复已完成';
                } else {
                    $actions[] = 'SQLite 修复未完全成功，请查看 data/backups/';
                }
            }
        }

        Database::clearPathCache();

        $ai = Database::connect('ai_learning.db');
        if ($ai) {
            $actions[] = 'AI 学习库 schema 已同步（decision_explanation 等列）';
        }

        $paper = Database::connect('paper_trading.db');
        if ($paper) {
            $actions[] = '模拟盘库 schema 已同步';
        }

        $health = self::healthCheck();
        return [
            'ok' => $health['ok'],
            'actions' => $actions,
            'health' => $health,
            'engine' => 'php',
        ];
    }

    public static function applyUpdateZip(string $zipPath, bool $backupFirst = true): array
    {
        if (!class_exists('ZipArchive')) {
            return ['ok' => false, 'error' => 'PHP ZipArchive 扩展未启用'];
        }
        if (!is_file($zipPath)) {
            return ['ok' => false, 'error' => '无效的 zip 文件'];
        }

        $zip = new ZipArchive();
        if ($zip->open($zipPath) !== true) {
            return ['ok' => false, 'error' => '无法打开 zip 文件'];
        }

        $backupInfo = null;
        if ($backupFirst) {
            $backupInfo = self::backup('pre_update');
            if (empty($backupInfo['ok'])) {
                $zip->close();
                return ['ok' => false, 'error' => '更新前备份失败', 'backup' => $backupInfo];
            }
        }

        $updated = [];
        $skipped = [];
        $blocked = [];

        for ($i = 0; $i < $zip->numFiles; $i++) {
            $name = $zip->getNameIndex($i);
            if ($name === false || str_ends_with($name, '/')) {
                continue;
            }
            $norm = ltrim(str_replace('\\', '/', $name), './');
            if (self::isProtected($norm)) {
                $blocked[] = $norm;
                continue;
            }
            if (!self::isAllowedUpdate($norm)) {
                $skipped[] = $norm;
                continue;
            }

            $target = PROJECT_ROOT . DIRECTORY_SEPARATOR . str_replace('/', DIRECTORY_SEPARATOR, $norm);
            $dir = dirname($target);
            if (!is_dir($dir) && !@mkdir($dir, 0755, true) && !is_dir($dir)) {
                $zip->close();
                return ['ok' => false, 'error' => "无法创建目录: {$dir}"];
            }

            $content = $zip->getFromIndex($i);
            if ($content === false) {
                continue;
            }
            if (file_put_contents($target, $content) === false) {
                $zip->close();
                return ['ok' => false, 'error' => "写入失败: {$norm}"];
            }
            $updated[] = $norm;
        }
        $zip->close();

        $fixResult = self::autoFix();

        return [
            'ok' => true,
            'updated' => $updated,
            'skipped' => array_slice($skipped, 0, 20),
            'blocked' => array_slice($blocked, 0, 20),
            'backup' => $backupInfo,
            'post_fix' => $fixResult,
            'engine' => 'php',
        ];
    }

    /** @return list<array{0: string, 1: string}> */
    private static function collectBackupFiles(): array
    {
        $files = [];

        if (is_file(CONFIG_PATH)) {
            $files[] = [CONFIG_PATH, 'config.yaml'];
        }

        foreach (array_merge(self::REQUIRED_DBS, self::OPTIONAL_DBS) as $dbName) {
            $path = Database::resolvePath($dbName) ?? (DATA_DIR . DIRECTORY_SEPARATOR . $dbName);
            if (is_file($path)) {
                $files[] = [$path, 'data/' . $dbName];
            }
        }

        $deploy = WEB_ROOT . DIRECTORY_SEPARATOR . 'deploy.local.php';
        if (is_file($deploy)) {
            $files[] = [$deploy, 'web/deploy.local.php'];
        }

        $epoch = DATA_DIR . DIRECTORY_SEPARATOR . 'pnl_epoch.json';
        if (is_file($epoch)) {
            $files[] = [$epoch, 'data/pnl_epoch.json'];
        }

        return $files;
    }

    /** @return list<string> */
    private static function migrateRootDbs(): array
    {
        $actions = [];
        if (!is_dir(DATA_DIR)) {
            @mkdir(DATA_DIR, 0755, true);
        }
        foreach (array_merge(self::REQUIRED_DBS, self::OPTIONAL_DBS) as $name) {
            $src = PROJECT_ROOT . DIRECTORY_SEPARATOR . $name;
            $dst = DATA_DIR . DIRECTORY_SEPARATOR . $name;
            if (is_file($src) && !is_file($dst)) {
                if (@copy($src, $dst)) {
                    Database::clearPathCache();
                    $actions[] = "已迁移 {$name} → data/";
                }
            }
        }
        return $actions;
    }

    /** @return list<string> */
    private static function ensureDirectories(): array
    {
        $dirs = [
            DATA_DIR,
            DATA_DIR . DIRECTORY_SEPARATOR . 'backups',
            DATA_DIR . DIRECTORY_SEPARATOR . 'updates',
            DATA_DIR . DIRECTORY_SEPARATOR . 'models',
        ];
        $actions = [];
        foreach ($dirs as $dir) {
            if (!is_dir($dir) && @mkdir($dir, 0755, true)) {
                $actions[] = '已创建目录: ' . basename($dir);
            }
        }
        if ($actions === []) {
            $actions[] = '目录结构已校验';
        }
        return $actions;
    }

    private static function checkPath(string $label, string $path, bool $required, bool $isDir = false): array
    {
        $exists = $isDir ? is_dir($path) : is_file($path);
        $ok = $exists || !$required;
        $level = ($required && !$exists) ? 'error' : ((!$required && !$exists) ? 'warn' : 'ok');
        return [
            'id' => $label,
            'ok' => $ok,
            'level' => $level,
            'message' => $label . ($exists ? ' 存在' : ' 缺失') . ($exists ? ": {$path}" : ''),
            'fixable' => $required && !$exists && $label === 'data 目录',
        ];
    }

    private static function checkDbSchema(): array
    {
        $pdo = Database::connect('ai_learning.db');
        if (!$pdo) {
            return ['id' => 'db_schema', 'ok' => false, 'level' => 'error', 'message' => 'ai_learning.db 不可用', 'fixable' => true];
        }
        $cols = Database::tableColumns($pdo, 'analysis_records');
        $required = ['decision_explanation', 'gate_reasons', 'raw_action', 'passed_gate'];
        $missing = array_values(array_diff($required, $cols));
        if ($missing !== []) {
            return [
                'id' => 'db_schema',
                'ok' => false,
                'level' => 'warn',
                'message' => 'analysis_records 缺少列: ' . implode(', ', $missing),
                'fixable' => true,
            ];
        }
        return ['id' => 'db_schema', 'ok' => true, 'level' => 'ok', 'message' => '数据库 schema 正常', 'fixable' => false];
    }

    private static function checkWebFiles(): array
    {
        $required = [
            'web/public/index.html',
            'web/public/api/index.php',
            'web/includes/bootstrap.php',
        ];
        $missing = [];
        foreach ($required as $rel) {
            $path = PROJECT_ROOT . DIRECTORY_SEPARATOR . str_replace('/', DIRECTORY_SEPARATOR, $rel);
            if (!is_file($path)) {
                $alt = WEB_ROOT . DIRECTORY_SEPARATOR . str_replace('web/', '', str_replace('/', DIRECTORY_SEPARATOR, $rel));
                if (!is_file($alt)) {
                    $missing[] = $rel;
                }
            }
        }
        if ($missing !== []) {
            return [
                'id' => 'web_files',
                'ok' => false,
                'level' => 'error',
                'message' => 'Web 文件缺失: ' . implode(', ', $missing),
                'fixable' => false,
            ];
        }
        return ['id' => 'web_files', 'ok' => true, 'level' => 'ok', 'message' => 'Web 控制台文件完整', 'fixable' => false];
    }

    private static function checkOrphanDbs(): array
    {
        $orphans = [];
        foreach (array_merge(self::REQUIRED_DBS, self::OPTIONAL_DBS) as $name) {
            $rootDb = PROJECT_ROOT . DIRECTORY_SEPARATOR . $name;
            $dataDb = DATA_DIR . DIRECTORY_SEPARATOR . $name;
            if (is_file($rootDb) && is_file($dataDb)) {
                $orphans[] = $name;
            }
        }
        if ($orphans !== []) {
            return [
                'id' => 'orphan_dbs',
                'ok' => false,
                'level' => 'warn',
                'message' => '根目录存在重复数据库: ' . implode(', ', $orphans),
                'fixable' => true,
            ];
        }
        return ['id' => 'orphan_dbs', 'ok' => true, 'level' => 'ok', 'message' => '数据库路径正常', 'fixable' => false];
    }

    private static function checkZipExtension(): array
    {
        $ok = class_exists('ZipArchive');
        return [
            'id' => 'php_zip',
            'ok' => $ok,
            'level' => $ok ? 'ok' : 'warn',
            'message' => $ok ? 'PHP ZipArchive 可用' : 'PHP ZipArchive 未启用（备份/热更新不可用）',
            'fixable' => false,
        ];
    }

    private static function checkWatcherHeartbeat(): array
    {
        $hb = DATA_DIR . DIRECTORY_SEPARATOR . 'watcher.heartbeat';
        if (!is_file($hb)) {
            return [
                'id' => 'watcher',
                'ok' => true,
                'level' => 'warn',
                'message' => '监控未运行（无 heartbeat 文件）',
                'fixable' => false,
            ];
        }
        $age = time() - (int)filemtime($hb);
        if ($age > 120) {
            return [
                'id' => 'watcher',
                'ok' => true,
                'level' => 'warn',
                'message' => "监控心跳过期 ({$age}s 前)",
                'fixable' => false,
            ];
        }
        return ['id' => 'watcher', 'ok' => true, 'level' => 'ok', 'message' => '模拟盘监控心跳正常', 'fixable' => false];
    }

    private static function isProtected(string $path): bool
    {
        $norm = str_replace('\\', '/', $path);
        foreach (self::UPDATE_PROTECTED as $p) {
            $p = rtrim($p, '/');
            if ($norm === $p || str_starts_with($norm, $p . '/')) {
                return true;
            }
        }
        return str_ends_with($norm, 'deploy.local.php');
    }

    private static function isAllowedUpdate(string $path): bool
    {
        $norm = str_replace('\\', '/', $path);
        foreach (self::UPDATE_ALLOW_PREFIXES as $prefix) {
            $prefix = rtrim($prefix, '/');
            if ($norm === $prefix || str_starts_with($norm, $prefix . '/')) {
                return true;
            }
        }
        return false;
    }
}
