<?php
declare(strict_types=1);

final class Database
{
    /** @var array<string, string|null> */
    private static array $pathCache = [];

    private static bool $repairAttempted = false;

    /** @var array<string, bool> */
    private static array $schemaReady = [];

    /** @var string[] */
    private static array $standardDbs = [
        'paper_trading.db',
        'ai_learning.db',
        'pattern_memory.db',
        'counterfactual.db',
        'trader_memory.db',
    ];

    public static function resolvePath(string $dbName): ?string
    {
        if (array_key_exists($dbName, self::$pathCache)) {
            return self::$pathCache[$dbName];
        }

        $candidates = [];

        // 标准库优先使用 data/ 目录，避免根目录旧库被永久缓存
        if (in_array($dbName, self::$standardDbs, true)) {
            $dataPath = DATA_DIR . DIRECTORY_SEPARATOR . $dbName;
            if (is_file($dataPath)) {
                return self::$pathCache[$dbName] = $dataPath;
            }
        }

        if ($dbName === 'paper_trading.db') {
            $cfg = Config::load();
            $configured = trim((string)($cfg['paper_trading']['db_path'] ?? ''));
            if ($configured !== '') {
                $candidates[] = self::absPath($configured);
            }
        }

        if ($dbName === 'trader_memory.db') {
            $cfg = Config::load();
            $configured = trim((string)($cfg['trader_council']['memory_db'] ?? ''));
            if ($configured !== '') {
                $candidates[] = self::absPath($configured);
            }
        }

        $candidates[] = DATA_DIR . DIRECTORY_SEPARATOR . $dbName;
        $candidates[] = PROJECT_ROOT . DIRECTORY_SEPARATOR . $dbName;
        $candidates[] = PROJECT_ROOT . DIRECTORY_SEPARATOR . 'data' . DIRECTORY_SEPARATOR . $dbName;

        foreach ($candidates as $path) {
            if ($path !== '' && is_file($path)) {
                return self::$pathCache[$dbName] = $path;
            }
        }

        return self::$pathCache[$dbName] = null;
    }

    public static function connect(string $dbName): ?PDO
    {
        return self::connectInternal($dbName, true);
    }

    private static function connectInternal(string $dbName, bool $allowRepair): ?PDO
    {
        $path = self::resolvePath($dbName);
        if ($path === null) {
            return null;
        }

        try {
            $pdo = new PDO('sqlite:' . $path, null, null, [
                PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
                PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
                PDO::ATTR_TIMEOUT => 30,
            ]);
            $pdo->exec('PRAGMA journal_mode=WAL');
            $pdo->exec('PRAGMA busy_timeout=30000');
            $pdo->exec('PRAGMA synchronous=NORMAL');
            $pdo->exec('PRAGMA temp_store=MEMORY');

            if ($dbName === 'ai_learning.db' && empty(self::$schemaReady[$dbName])) {
                self::ensureAiLearningSchema($pdo);
                self::$schemaReady[$dbName] = true;
            }
            if ($dbName === 'paper_trading.db' && empty(self::$schemaReady[$dbName])) {
                self::ensurePaperTradingSchema($pdo);
                self::$schemaReady[$dbName] = true;
            }

            return $pdo;
        } catch (Throwable $e) {
            $msg = $e->getMessage();
            $corrupt = stripos($msg, 'malformed') !== false
                || stripos($msg, 'disk image') !== false
                || stripos($msg, 'file is not a database') !== false;

            if ($allowRepair && $corrupt && !self::$repairAttempted && class_exists('Bridge')) {
                self::$repairAttempted = true;
                self::clearPathCache();
                Bridge::repairDatabases();
                self::clearPathCache();
                return self::connectInternal($dbName, false);
            }

            return null;
        }
    }

    public static function clearPathCache(): void
    {
        self::$pathCache = [];
        self::$schemaReady = [];
    }

    /** @return string[] */
    public static function tableColumns(PDO $pdo, string $table): array
    {
        $rows = self::rows($pdo, 'PRAGMA table_info(' . preg_replace('/[^a-zA-Z0-9_]/', '', $table) . ')');
        return array_column($rows, 'name');
    }

    public static function ensureColumn(PDO $pdo, string $table, string $column, string $type): void
    {
        $safeTable = preg_replace('/[^a-zA-Z0-9_]/', '', $table);
        $safeCol = preg_replace('/[^a-zA-Z0-9_]/', '', $column);
        if ($safeTable === '' || $safeCol === '') {
            return;
        }
        $existing = self::tableColumns($pdo, $safeTable);
        if (in_array($safeCol, $existing, true)) {
            return;
        }
        $pdo->exec("ALTER TABLE {$safeTable} ADD COLUMN {$safeCol} {$type}");
    }

    private static function ensureAiLearningSchema(PDO $pdo): void
    {
        try {
            self::ensureColumn($pdo, 'analysis_records', 'decision_explanation', 'TEXT');
            self::ensureColumn($pdo, 'analysis_records', 'gate_reasons', 'TEXT');
            self::ensureColumn($pdo, 'analysis_records', 'raw_action', 'TEXT');
            self::ensureColumn($pdo, 'analysis_records', 'passed_gate', 'INTEGER');
            self::ensureColumn($pdo, 'analysis_records', 'market_regime', 'TEXT');
            self::ensureColumn($pdo, 'analysis_records', 'quality_score', 'INTEGER');
            self::ensureColumn($pdo, 'analysis_records', 'quality_tier', 'TEXT');
            self::ensureColumn($pdo, 'strategy_performance', 'weighted_correct', 'REAL DEFAULT 0');
            $pdo->exec("
                CREATE TABLE IF NOT EXISTS meta_learning_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    cards_before INTEGER,
                    cards_after INTEGER,
                    merged_count INTEGER,
                    summary TEXT
                )
            ");
        } catch (Throwable $e) {
            // 只读场景下迁移失败不阻断 API
        }
    }

    private static function ensurePaperTradingSchema(PDO $pdo): void
    {
        try {
            self::ensureColumn($pdo, 'paper_positions', 'signal_tracking_id', 'INTEGER');
            self::ensureColumn($pdo, 'paper_positions', 'r_multiple', 'REAL');
        } catch (Throwable $e) {
            // 只读场景下迁移失败不阻断 API
        }
    }

    public static function rows(PDO $pdo, string $sql, array $params = []): array
    {
        $stmt = $pdo->prepare($sql);
        $stmt->execute($params);
        return $stmt->fetchAll();
    }

    public static function row(PDO $pdo, string $sql, array $params = []): ?array
    {
        $stmt = $pdo->prepare($sql);
        $stmt->execute($params);
        $row = $stmt->fetch();
        return $row === false ? null : $row;
    }

    private static function absPath(string $path): string
    {
        if ($path === '') {
            return '';
        }
        if (preg_match('/^[A-Za-z]:[\\\\\\/]/', $path) || str_starts_with($path, '/')) {
            return str_replace(['/', '\\'], DIRECTORY_SEPARATOR, $path);
        }
        return PROJECT_ROOT . DIRECTORY_SEPARATOR . str_replace(['/', '\\'], DIRECTORY_SEPARATOR, $path);
    }
}
