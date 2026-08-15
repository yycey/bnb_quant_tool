<?php
declare(strict_types=1);

require_once dirname(__DIR__, 2) . '/includes/bootstrap.php';

require_api_token();

$endpoint = $_GET['endpoint'] ?? '';
if ($endpoint === '' && isset($_SERVER['PATH_INFO'])) {
    $endpoint = trim((string)$_SERVER['PATH_INFO'], '/');
}
if ($endpoint === '') {
    $endpoint = 'overview';
}

try {
    switch ($endpoint) {
        case 'overview':
            handleOverview();
            break;
        case 'positions':
            handlePositions();
            break;
        case 'history':
            handleHistory();
            break;
        case 'signals':
            handleSignals();
            break;
        case 'ai_learning':
            handleAiLearning();
            break;
        case 'config':
            handleConfig();
            break;
        case 'config_schema':
            Response::json(Config::editableSchema());
            break;
        case 'config_update':
            handleConfigUpdate();
            break;
        case 'strategies':
            handleStrategies();
            break;
        case 'monitor':
            handleMonitor();
            break;
        case 'performance':
            handlePerformance();
            break;
        case 'market':
            $cfg = Config::load();
            $symbol = $cfg['trading']['symbol'] ?? 'BNBUSDT';
            $snap = Market::snapshot($symbol);
            $snap['symbol'] = $symbol;
            $snap['checked_at'] = date('c');
            Response::json($snap);
            break;
        case 'close_position':
            handleClosePosition();
            break;
        case 'stats':
            handleStats();
            break;
        case 'status':
            handleStatus();
            break;
        case 'latest_advice':
            handleLatestAdvice();
            break;
        case 'decision_history':
            handleDecisionHistory();
            break;
        case 'run_analysis':
            handleRunAnalysis();
            break;
        case 'circuit_breaker':
            handleCircuitBreaker();
            break;
        case 'scan_signals':
            handleScanSignals();
            break;
        case 'maintenance':
            handleMaintenance();
            break;
        case 'maintenance_upload':
            handleMaintenanceUpload();
            break;
        case 'pnl_epoch':
            handlePnlEpoch();
            break;
        case 'loop_health':
            handleLoopHealth();
            break;
        case 'council_memory':
            handleCouncilMemory();
            break;
        default:
            Response::error('Unknown endpoint: ' . $endpoint, 404);
    }
} catch (Throwable $e) {
    Response::error($e->getMessage() ?: get_class($e), 500);
}

function rowVal(?array $row, string $key, $default = null)
{
    return $row !== null && array_key_exists($key, $row) ? $row[$key] : $default;
}

function analysisRecordsSelect(PDO $pdo): string
{
    $cols = [
        'id', 'timestamp', 'symbol', 'timeframe', 'current_price', 'final_signal',
        'ai_signal', 'ai_confidence', 'ai_analysis', 'trading_action',
        'entry_price', 'stop_loss', 'take_profit', 'risk_passed', 'risk_reason',
        'buy_signals', 'sell_signals', 'hold_signals', 'consensus_confidence',
    ];
    $optional = [
        'raw_action', 'decision_explanation', 'gate_reasons', 'passed_gate',
        'market_regime', 'trade_advice_snapshot', 'market_regime_json',
        'multi_agent_deliberation',
    ];
    $existing = Database::tableColumns($pdo, 'analysis_records');
    foreach ($optional as $col) {
        if (in_array($col, $existing, true)) {
            $cols[] = $col;
        }
    }
    return implode(', ', $cols);
}

function handleOverview(): void
{
    $cfg = Config::load();
    $result = [
        'symbol' => $cfg['trading']['symbol'] ?? 'BNBUSDT',
        'account_balance' => $cfg['trading']['account_balance'] ?? 0,
        'confidence_threshold' => $cfg['trading']['confidence_threshold']
            ?? ($cfg['analysis']['confidence_threshold'] ?? 0),
        'autopilot_mode' => $cfg['autopilot']['mode'] ?? 'off',
        'autopilot_interval' => (int)($cfg['autopilot']['interval_minutes'] ?? 0),
        'require_gate_pass' => (bool)($cfg['ai_trading']['require_gate_pass'] ?? true),
        'auto_run_enabled' => (bool)($cfg['auto_run']['enabled'] ?? false),
        'auto_run_interval' => $cfg['auto_run']['interval_minutes'] ?? 0,
        'scanner_enabled' => (bool)($cfg['signal_scanner']['enabled'] ?? false),
        'scanner_interval' => $cfg['signal_scanner']['scan_interval'] ?? 0,
        'auto_follow_enabled' => (bool)($cfg['paper_trading']['auto_follow'] ?? false),
        'follow_ai_direction' => (bool)($cfg['ai_trading']['follow_ai_direction'] ?? false),
        'intelligence_loop_enabled' => (($cfg['intelligence_loop']['enabled'] ?? true) !== false),
        'reuse_enabled' => (bool)(($cfg['capability_memory']['reuse_known_situation'] ?? false)),
        'ai_providers' => [
            'deepseek' => (bool)(($cfg['deepseek']['enabled'] ?? false)),
            'qianwen' => (bool)(($cfg['qianwen']['enabled'] ?? false)),
            'volcengine' => (bool)(($cfg['volcengine']['enabled'] ?? false)),
        ],
        'validation_trading' => (bool)(($cfg['validation_trading']['enabled'] ?? false)),
        'timestamp' => gmdate('c'),
        'open_positions' => 0,
        'closed_positions' => 0,
        'win_rate' => 0,
        'avg_r' => 0,
        'max_r' => 0,
        'min_r' => 0,
        'total_trades' => 0,
        'last_trade_time' => '-',
        'ai_analysis_count' => 0,
        'ai_learning_count' => 0,
        'pnl_epoch' => PnlEpoch::info(),
    ];

    $epochFilter = PnlEpoch::closedFilter();

    $pdo = Database::connect('paper_trading.db');
    if ($pdo) {
        $row = Database::row($pdo, "SELECT COUNT(*) AS cnt FROM paper_positions WHERE status='OPEN'");
        $result['open_positions'] = (int)($row['cnt'] ?? 0);

        $row = Database::row($pdo, "SELECT COUNT(*) AS cnt FROM paper_positions WHERE status='CLOSED'");
        $result['closed_positions_all'] = (int)($row['cnt'] ?? 0);
        $result['closed_positions'] = $result['closed_positions_all'];

        $row = Database::row($pdo, "
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN COALESCE(realized_pnl_usdt, 0) > 0.01 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN COALESCE(realized_pnl_usdt, 0) < -0.01 THEN 1 ELSE 0 END) AS losses,
                   AVG(CASE WHEN r_multiple IS NOT NULL THEN r_multiple END) AS avg_r,
                   AVG(CASE WHEN realized_pnl_usdt > 0.01 AND r_multiple IS NOT NULL THEN r_multiple END) AS avg_win_r,
                   AVG(CASE WHEN realized_pnl_usdt < -0.01 AND r_multiple IS NOT NULL THEN r_multiple END) AS avg_loss_r,
                   MAX(r_multiple) AS max_r,
                   MIN(r_multiple) AS min_r,
                   SUM(realized_pnl_usdt) AS total_pnl,
                   SUM(CASE WHEN COALESCE(mfe_r, 0) >= 0.5 AND COALESCE(realized_pnl_usdt, 0) < -0.01 THEN 1 ELSE 0 END) AS gave_back
            FROM paper_positions
            WHERE status='CLOSED'
              AND (close_reason IS NULL OR close_reason NOT LIKE 'MANUAL%')
              {$epochFilter['sql']}
        ", $epochFilter['params']);
        $closedTotal = (int)($row['total'] ?? 0);
        $wins = (int)($row['wins'] ?? 0);
        $losses = (int)($row['losses'] ?? 0);
        $decided = $wins + $losses;
        if ($closedTotal > 0) {
            $result['total_trades'] = $closedTotal;
            $result['win_rate'] = $decided > 0 ? round(($wins / $decided) * 100, 1) : 0;
            $result['avg_r'] = round((float)$row['avg_r'], 3);
            $result['expectancy_r'] = $result['avg_r'];
            $result['avg_win_r'] = round((float)($row['avg_win_r'] ?? 0), 3);
            $result['avg_loss_r'] = round((float)($row['avg_loss_r'] ?? 0), 3);
            $result['max_r'] = round((float)$row['max_r'], 3);
            $result['min_r'] = round((float)$row['min_r'], 3);
            $result['auto_pnl'] = round((float)($row['total_pnl'] ?? 0), 2);
            $result['gave_back_count'] = (int)($row['gave_back'] ?? 0);
            $result['stats_scope'] = 'auto_only';
        }

        $row = Database::row($pdo, "SELECT MAX(closed_at) AS last_close FROM paper_positions WHERE status='CLOSED'");
        $result['last_trade_time'] = rowVal($row, 'last_close', '-');
    }

    $ai = Database::connect('ai_learning.db');
    if ($ai) {
        $row = Database::row($ai, 'SELECT COUNT(*) AS cnt FROM analysis_records');
        $result['ai_analysis_count'] = (int)($row['cnt'] ?? 0);
        $row = Database::row($ai, 'SELECT COUNT(*) AS cnt FROM learning_log');
        $result['ai_learning_count'] = (int)($row['cnt'] ?? 0);
        $result['growth'] = AiGrowth::growthSnapshot($ai);
        $result['strategy_contribution'] = AiGrowth::strategyContribution($ai, 8);
        $result['factor_attribution'] = AiGrowth::factorAttribution($ai, 8);
        $result['shadow_param_trials'] = AiGrowth::shadowParamTrials($ai, 5);
        $result['agent_accuracy'] = AiGrowth::agentAccuracy($ai);
    } else {
        $result['growth'] = AiGrowth::growthSnapshot(null);
        $result['strategy_contribution'] = [];
        $result['factor_attribution'] = [];
        $result['shadow_param_trials'] = [];
        $result['agent_accuracy'] = [];
    }

    $paper = Database::connect('paper_trading.db');
    $trader = Database::connect('trader_memory.db');
    $result['loop_health'] = IntelligenceLoop::health($ai ?: null, $paper ?: null, $trader ?: null);
    $result['council_memory'] = IntelligenceLoop::councilMemory($trader ?: null, 12);
    $result['llm'] = $result['loop_health']['llm'] ?? IntelligenceLoop::llmSummary($cfg);
    $result['reuse'] = $result['loop_health']['reuse'] ?? IntelligenceLoop::reuseSummary($cfg);

    $symbol = (string)($result['symbol'] ?? 'BNBUSDT');
    $market = Market::snapshot($symbol);
    $result['current_price'] = (float)($market['price'] ?? 0);
    $result['change_24h'] = (float)($market['change_24h'] ?? 0);
    $result['price_source'] = (string)($market['source'] ?? 'none');
    // 仅在实时行情失败时，用「同品种、30分钟内」的分析价兜底；过期分析价（如假604）绝不展示
    if ($result['current_price'] <= 0 && $ai) {
        $row = Database::row(
            $ai,
            "SELECT current_price, timestamp, symbol FROM analysis_records
             WHERE UPPER(COALESCE(symbol,'')) = UPPER(?)
             ORDER BY id DESC LIMIT 1",
            [$symbol]
        );
        $fallback = (float)($row['current_price'] ?? 0);
        $ts = (string)($row['timestamp'] ?? '');
        $ageOk = false;
        if ($ts !== '') {
            try {
                $ageOk = (time() - (new DateTimeImmutable($ts))->getTimestamp()) <= 1800;
            } catch (Throwable $e) {
                $ageOk = false;
            }
        }
        if ($fallback > 0 && $ageOk) {
            $result['current_price'] = $fallback;
            $result['price_source'] = 'analysis_fallback';
        }
    }

    Response::json($result);
}

function handlePositions(): void
{
    $pdo = Database::connect('paper_trading.db');
    if (!$pdo) {
        Response::json([]);
    }

    try {
        $rows = Database::rows($pdo, "
            SELECT id, symbol, side, side AS action, entry_price, qty_total AS quantity, qty_remaining,
                   sl AS stop_loss, tp1 AS take_profit, tp1 AS take_profit1, tp2 AS take_profit2, tp3 AS take_profit3,
                   opened_at, signal_tracking_id, r_multiple, realized_pnl_usdt,
                   tp1_hit, tp2_hit, tp3_hit, leverage
            FROM paper_positions
            WHERE status='OPEN'
            ORDER BY opened_at DESC
        ");
    } catch (Throwable $e) {
        Response::json([]);
    }

    $cfg = Config::load();
    $feeRate = (float)($cfg['backtest']['fee_rate'] ?? 0.0004);
    $defaultSymbol = $cfg['trading']['symbol'] ?? 'BNBUSDT';
    $prices = [];

    foreach ($rows as &$row) {
        $sym = $row['symbol'] ?: $defaultSymbol;
        if (!isset($prices[$sym])) {
            $prices[$sym] = Market::lastPrice($sym);
        }
        $price = (float)$prices[$sym];
        if ($price <= 0) {
            $row['mark_price'] = null;
            $row['unrealized_pnl_usdt'] = null;
            continue;
        }
        $row['mark_price'] = $price;
        $row['unrealized_pnl_usdt'] = calcUnrealizedPnl(
            (string)$row['action'],
            (float)$row['entry_price'],
            (float)$row['qty_remaining'],
            $price,
            $feeRate
        );
    }
    unset($row);

    Response::json($rows);
}

function calcUnrealizedPnl(string $side, float $entry, float $qty, float $price, float $feeRate): float
{
    if ($entry <= 0 || $qty <= 0 || $price <= 0) {
        return 0.0;
    }
    $fee = $price * $qty * $feeRate;
    $side = strtoupper($side);
    if ($side === 'LONG') {
        return round(($price - $entry) * $qty - $fee, 4);
    }
    return round(($entry - $price) * $qty - $fee, 4);
}

function handleHistory(): void
{
    $limit = max(1, min(200, (int)($_GET['limit'] ?? 50)));
    $pdo = Database::connect('paper_trading.db');
    if (!$pdo) {
        Response::json(['items' => [], 'pnl_epoch' => PnlEpoch::info()]);
        return;
    }

    try {
        $rows = Database::rows($pdo, "
            SELECT id, side AS action, entry_price, close_avg_price, qty_total AS quantity,
                   realized_pnl_usdt AS pnl, r_multiple,
                   opened_at, closed_at, close_reason, tp1_hit, tp2_hit, tp3_hit
            FROM paper_positions
            WHERE status='CLOSED'
            ORDER BY closed_at DESC
            LIMIT ?
        ", [$limit]);
        Response::json([
            'items' => $rows,
            'pnl_epoch' => PnlEpoch::info(),
        ]);
    } catch (Throwable $e) {
        Response::json(['items' => [], 'pnl_epoch' => PnlEpoch::info()]);
    }
}

function handleSignals(): void
{
    $pdo = Database::connect('paper_trading.db');
    if (!$pdo) {
        Response::json([]);
    }

    try {
        $rows = Database::rows($pdo, "
            SELECT id, generated_at AS created_at, symbol, direction, entry_price,
                   confidence, strength, market_regime, followed,
                   actual_pnl_usdt, actual_exit, exit_reason, feedback_at
            FROM signal_tracking
            ORDER BY generated_at DESC
            LIMIT 100
        ");
        Response::json($rows);
    } catch (Throwable $e) {
        Response::json([]);
    }
}

function handleAiLearning(): void
{
    $result = [
        'analysis_count' => 0,
        'learning_count' => 0,
        'strategy_count' => 0,
        'recent_logs' => [],
    ];

    $pdo = Database::connect('ai_learning.db');
    if (!$pdo) {
        Response::json($result);
    }

    $row = Database::row($pdo, 'SELECT COUNT(*) AS cnt FROM analysis_records');
    $result['analysis_count'] = (int)($row['cnt'] ?? 0);

    $row = Database::row($pdo, 'SELECT COUNT(*) AS cnt FROM learning_log');
    $result['learning_count'] = (int)($row['cnt'] ?? 0);

    try {
        $row = Database::row($pdo, 'SELECT COUNT(*) AS cnt FROM strategy_performance');
        $result['strategy_count'] = (int)($row['cnt'] ?? 0);
    } catch (Throwable $e) {
        // table may not exist
    }

    try {
        $result['recent_logs'] = Database::rows($pdo, "
            SELECT timestamp, event_type AS action, details, improvement_score AS outcome
            FROM learning_log
            ORDER BY timestamp DESC
            LIMIT 20
        ");
    } catch (Throwable $e) {
        $result['recent_logs'] = [];
    }

    $result['growth'] = AiGrowth::growthSnapshot($pdo);
    $result['strategy_contribution'] = AiGrowth::strategyContribution($pdo, 10);
    $result['factor_attribution'] = AiGrowth::factorAttribution($pdo, 12);
    $result['shadow_param_trials'] = AiGrowth::shadowParamTrials($pdo, 8);
    $result['agent_accuracy'] = AiGrowth::agentAccuracy($pdo);

    $snap = Bridge::learningSnapshot();
    if (is_array($snap) && !empty($snap['ok'])) {
        $result['learning_snapshot'] = $snap;
        $result['win_rate_context'] = $snap['win_rate_context'] ?? null;
        $result['loss_patterns'] = $snap['loss_patterns'] ?? [];
        if (!empty($snap['profit_curve']) && is_array($snap['profit_curve'])) {
            $result['profit_curve_text'] = $snap['profit_curve']['curve_text'] ?? '';
        }
    }

    Response::json($result);
}

function handlePerformance(): void
{
    $pdo = Database::connect('paper_trading.db');
    if (!$pdo) {
        Response::json(['daily' => [], 'weekly' => [], 'pnl_epoch' => PnlEpoch::info()]);
        return;
    }

    $epochFilter = PnlEpoch::closedFilter();

    try {
        $daily = Database::rows($pdo, "
            SELECT DATE(closed_at) AS day,
                   COUNT(*) AS trades,
                   SUM(CASE WHEN realized_pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(realized_pnl_usdt) AS total_pnl,
                   AVG(r_multiple) AS avg_r
            FROM paper_positions
            WHERE status='CLOSED' AND closed_at IS NOT NULL
              AND closed_at >= datetime('now', '-30 days'){$epochFilter['sql']}
            GROUP BY DATE(closed_at)
            ORDER BY day DESC
        ", $epochFilter['params']);

        $weekly = Database::rows($pdo, "
            SELECT strftime('%Y-W%W', closed_at) AS week,
                   COUNT(*) AS trades,
                   SUM(CASE WHEN realized_pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(realized_pnl_usdt) AS total_pnl,
                   AVG(r_multiple) AS avg_r
            FROM paper_positions
            WHERE status='CLOSED' AND closed_at IS NOT NULL
              AND closed_at >= datetime('now', '-84 days'){$epochFilter['sql']}
            GROUP BY strftime('%Y-W%W', closed_at)
            ORDER BY week DESC
        ", $epochFilter['params']);

        Response::json(['daily' => $daily, 'weekly' => $weekly, 'pnl_epoch' => PnlEpoch::info()]);
    } catch (Throwable $e) {
        Response::json(['daily' => [], 'weekly' => [], 'pnl_epoch' => PnlEpoch::info()]);
    }
}

function handleStats(): void
{
    $empty = [
        'open_count' => 0,
        'total_trades' => 0,
        'win_rate' => 0,
        'total_realized_pnl' => 0,
        'avg_win_usdt' => 0,
        'avg_loss_usdt' => 0,
        'best_trade_usdt' => 0,
        'worst_trade_usdt' => 0,
        'expectancy_r' => 0,
        'avg_win_r' => 0,
        'avg_loss_r' => 0,
        'avg_r' => 0,
        'profit_factor' => 0,
        'gave_back_count' => 0,
        'auto_only' => true,
        'manual_excluded' => 0,
        'pnl_epoch' => PnlEpoch::info(),
        'closed_positions_all' => 0,
    ];

    $pdo = Database::connect('paper_trading.db');
    if (!$pdo) {
        Response::json($empty);
        return;
    }

    $epochFilter = PnlEpoch::closedFilter();

    try {
        $allRow = Database::row($pdo, "SELECT COUNT(*) AS cnt FROM paper_positions WHERE status='CLOSED'");

        $row = Database::row($pdo, "
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN realized_pnl_usdt > 0.01 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN realized_pnl_usdt < -0.01 THEN 1 ELSE 0 END) AS losses,
                   SUM(realized_pnl_usdt) AS total_pnl,
                   AVG(CASE WHEN realized_pnl_usdt > 0.01 THEN realized_pnl_usdt END) AS avg_win,
                   AVG(CASE WHEN realized_pnl_usdt < -0.01 THEN realized_pnl_usdt END) AS avg_loss,
                   MAX(realized_pnl_usdt) AS best,
                   MIN(realized_pnl_usdt) AS worst,
                   AVG(CASE WHEN r_multiple IS NOT NULL THEN r_multiple END) AS avg_r,
                   AVG(CASE WHEN realized_pnl_usdt > 0.01 AND r_multiple IS NOT NULL THEN r_multiple END) AS avg_win_r,
                   AVG(CASE WHEN realized_pnl_usdt < -0.01 AND r_multiple IS NOT NULL THEN r_multiple END) AS avg_loss_r,
                   SUM(CASE WHEN realized_pnl_usdt > 0.01 THEN realized_pnl_usdt ELSE 0 END) AS gross_win,
                   SUM(CASE WHEN realized_pnl_usdt < -0.01 THEN ABS(realized_pnl_usdt) ELSE 0 END) AS gross_loss,
                   SUM(CASE WHEN COALESCE(mfe_r, 0) >= 0.5 AND realized_pnl_usdt < -0.01 THEN 1 ELSE 0 END) AS gave_back
            FROM paper_positions
            WHERE status='CLOSED'
              AND (close_reason IS NULL OR close_reason NOT LIKE 'MANUAL%')
              {$epochFilter['sql']}
        ", $epochFilter['params']);

        $open = Database::row($pdo, "SELECT COUNT(*) AS cnt FROM paper_positions WHERE status='OPEN'");
        $total = (int)($row['total'] ?? 0);
        $wins = (int)($row['wins'] ?? 0);
        $losses = (int)($row['losses'] ?? 0);
        $decided = $wins + $losses;
        $grossWin = (float)($row['gross_win'] ?? 0);
        $grossLoss = (float)($row['gross_loss'] ?? 0);
        $pf = $grossLoss > 0 ? ($grossWin / $grossLoss) : ($grossWin > 0 ? $grossWin : 0);
        $allClosed = (int)($allRow['cnt'] ?? 0);

        Response::json([
            'open_count' => (int)($open['cnt'] ?? 0),
            'total_trades' => $total,
            'win_rate' => $decided > 0 ? round($wins / $decided, 4) : 0,
            'total_realized_pnl' => round((float)($row['total_pnl'] ?? 0), 2),
            'avg_win_usdt' => round((float)($row['avg_win'] ?? 0), 2),
            'avg_loss_usdt' => round((float)($row['avg_loss'] ?? 0), 2),
            'best_trade_usdt' => round((float)($row['best'] ?? 0), 2),
            'worst_trade_usdt' => round((float)($row['worst'] ?? 0), 2),
            'expectancy_r' => round((float)($row['avg_r'] ?? 0), 4),
            'avg_r' => round((float)($row['avg_r'] ?? 0), 4),
            'avg_win_r' => round((float)($row['avg_win_r'] ?? 0), 4),
            'avg_loss_r' => round((float)($row['avg_loss_r'] ?? 0), 4),
            'profit_factor' => round($pf, 3),
            'gave_back_count' => (int)($row['gave_back'] ?? 0),
            'auto_only' => true,
            'manual_excluded' => max(0, $allClosed - $total),
            'pnl_epoch' => PnlEpoch::info(),
            'closed_positions_all' => $allClosed,
        ]);
    } catch (Throwable $e) {
        Response::json($empty);
    }
}

function handlePnlEpoch(): void
{
    if ($_SERVER['REQUEST_METHOD'] === 'GET') {
        $info = PnlEpoch::info();
        $pdo = Database::connect('paper_trading.db');
        if ($pdo) {
            $epochFilter = PnlEpoch::closedFilter();
            $row = Database::row($pdo, "
                SELECT COUNT(*) AS cnt, COALESCE(SUM(realized_pnl_usdt), 0) AS pnl
                FROM paper_positions
                WHERE status='CLOSED'{$epochFilter['sql']}
            ", $epochFilter['params']);
            $allRow = Database::row($pdo, "SELECT COUNT(*) AS cnt FROM paper_positions WHERE status='CLOSED'");
            $info['epoch_trades'] = (int)($row['cnt'] ?? 0);
            $info['epoch_pnl'] = round((float)($row['pnl'] ?? 0), 2);
            $info['all_trades'] = (int)($allRow['cnt'] ?? 0);
        }
        Response::json($info);
        return;
    }

    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        Response::error('GET/POST only', 405);
    }
    require_write_token();

    $input = json_decode(file_get_contents('php://input') ?: '{}', true);
    if (!is_array($input)) {
        $input = $_POST;
    }
    $action = (string)($input['action'] ?? 'reset_today');

    if ($action === 'clear') {
        $result = PnlEpoch::clear();
    } elseif ($action === 'reset_now') {
        $result = PnlEpoch::resetFromNow();
    } else {
        $result = PnlEpoch::resetFromToday();
    }

    if (empty($result['ok'])) {
        Response::error((string)($result['error'] ?? '操作失败'), 400);
    }
    Response::json($result);
}

function handleClosePosition(): void
{
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        Response::error('POST required', 405);
    }
    require_write_token();

    $input = json_decode(file_get_contents('php://input') ?: '{}', true);
    if (!is_array($input)) {
        $input = $_POST;
    }

    $pid = (int)($input['id'] ?? 0);
    if ($pid <= 0) {
        Response::error('Missing position id');
    }

    $price = (float)($input['price'] ?? 0);

    // 优先走 Python 引擎，保证学习闭环完整
    $bridge = Bridge::closePosition($pid, $price);
    if ($bridge !== null) {
        if (!empty($bridge['ok'])) {
            Response::json($bridge);
        }
        Response::error($bridge['error'] ?? 'Close failed', 400);
    }

    // Python 不可用时回退直连 DB
    handleClosePositionFallback($pid, $price);
}

function handleClosePositionFallback(int $pid, float $price): void
{
    $pdo = Database::connect('paper_trading.db');
    if (!$pdo) {
        Response::error('Database not found', 500);
    }

    $row = Database::row($pdo, "SELECT * FROM paper_positions WHERE id=? AND status='OPEN'", [$pid]);
    if (!$row) {
        Response::error('Position not found or already closed', 404);
    }

    $cfg = Config::load();
    $symbol = $row['symbol'] ?: ($cfg['trading']['symbol'] ?? 'BNBUSDT');
    if ($price <= 0) {
        $price = Market::lastPrice($symbol);
    }
    if ($price <= 0) {
        Response::error('Unable to fetch market price');
    }

    $feeRate = (float)($cfg['backtest']['fee_rate'] ?? 0.0004);
    $pnl = calcUnrealizedPnl(
        (string)$row['side'],
        (float)$row['entry_price'],
        (float)$row['qty_remaining'],
        $price,
        $feeRate
    );

    $newRealized = (float)$row['realized_pnl_usdt'] + $pnl;
    $entry = (float)$row['entry_price'];
    $sl0 = (float)$row['sl_initial'];
    $riskTotal = abs($entry - $sl0) * (float)$row['qty_total'];
    $rMult = $riskTotal > 0 ? round($newRealized / $riskTotal, 3) : null;
    $now = date('Y-m-d\TH:i:s');
    $qty = (float)$row['qty_remaining'];
    $fee = $price * $qty * $feeRate;

    $pdo->beginTransaction();
    try {
        $attempts = 0;
        $updated = false;
        $lastErr = null;
        while ($attempts < 8) {
            try {
                if (!$pdo->inTransaction()) {
                    $pdo->beginTransaction();
                }
                $stmt = $pdo->prepare("
                    UPDATE paper_positions
                    SET qty_remaining=0, realized_pnl_usdt=?, status='CLOSED',
                        closed_at=?, close_avg_price=?, close_reason='MANUAL_WEB', r_multiple=?
                    WHERE id=? AND status='OPEN'
                ");
                $stmt->execute([$newRealized, $now, $price, $rMult, $pid]);
                $updated = $stmt->rowCount() > 0;
                if (!$updated) {
                    $pdo->rollBack();
                    Response::error('Position not found or already closed', 409);
                }
                $pdo->prepare("
                    INSERT INTO paper_fills (position_id, ts, fill_type, price, qty, fee, pnl)
                    VALUES (?, ?, 'MANUAL_WEB', ?, ?, ?, ?)
                ")->execute([$pid, $now, $price, $qty, $fee, $pnl]);
                $pdo->commit();
                $lastErr = null;
                break;
            } catch (Throwable $e) {
                $lastErr = $e;
                if ($pdo->inTransaction()) {
                    $pdo->rollBack();
                }
                $msg = strtolower($e->getMessage());
                if (strpos($msg, 'locked') === false && strpos($msg, 'busy') === false) {
                    throw $e;
                }
                $attempts++;
                usleep(100000 * (1 << min($attempts, 4)));
            }
        }
        if ($lastErr !== null) {
            throw $lastErr;
        }
        if (!$updated) {
            Response::error('Position not found, already closed, or database locked', 409);
        }
    } catch (Throwable $e) {
        if ($pdo->inTransaction()) {
            $pdo->rollBack();
        }
        throw $e;
    }

    Response::json([
        'ok' => true,
        'id' => $pid,
        'price' => $price,
        'pnl' => round($pnl, 4),
        'r_multiple' => $rMult,
        'fallback' => true,
    ]);
}

function handleConfig(): void
{
    Config::reload();
    Response::json([
        'config' => Config::loadRedacted(),
        'schema' => Config::editableSchema(),
        'auth_required' => api_token_configured() !== '',
    ]);
}

function handleConfigUpdate(): void
{
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        Response::error('POST required', 405);
    }
    require_write_token();

    $input = json_decode(file_get_contents('php://input') ?: '{}', true);
    if (!is_array($input)) {
        Response::error('Invalid JSON body');
    }

    $patch = $input['patch'] ?? $input;
    if (!is_array($patch)) {
        Response::error('Missing patch object');
    }

    $result = Config::patch($patch);
    if (!$result['ok']) {
        Response::json([
            'ok' => false,
            'errors' => $result['errors'],
        ], 400);
    }

    Response::json([
        'ok' => true,
        'updated' => $result['updated'],
        'errors' => $result['errors'],
        'backup' => $result['backup'] ?? null,
        'config' => Config::loadRedacted(),
    ]);
}

function handleStrategies(): void
{
    $pdo = Database::connect('ai_learning.db');
    if (!$pdo) {
        Response::json([]);
    }

    try {
        $rows = Database::rows($pdo, "
            SELECT strategy_name, total_predictions, correct_predictions, win_rate,
                   weight, is_active, streak_current, last_updated
            FROM strategy_performance
            WHERE is_active = 1 AND strategy_name NOT LIKE 'paper_%'
            ORDER BY weight DESC, win_rate DESC
        ");
        $contrib = AiGrowth::strategyContribution($pdo, 50);
        $contribMap = [];
        foreach ($contrib as $c) {
            $contribMap[$c['strategy_name']] = $c;
        }
        foreach ($rows as &$row) {
            $name = (string)($row['strategy_name'] ?? '');
            $extra = $contribMap[$name] ?? null;
            if ($extra) {
                $row['contribution_pct'] = $extra['contribution_pct'];
                $row['impact_score'] = $extra['impact_score'];
                $row['sample_factor'] = $extra['sample_factor'];
            }
        }
        unset($row);
        Response::json($rows);
    } catch (Throwable $e) {
        Response::json([]);
    }
}

function handleMonitor(): void
{
    $cfg = Config::load();
    $hbPath = DATA_DIR . DIRECTORY_SEPARATOR . 'watcher.heartbeat';
    $lastBeat = null;
    $watcherRunning = false;
    $watcherAgeSec = null;

    if (is_file($hbPath)) {
        $raw = trim((string)file_get_contents($hbPath));
        $decoded = json_decode($raw, true);
        if (is_array($decoded) && !empty($decoded['ts'])) {
            $lastBeat = (string)$decoded['ts'];
            $priceFails = $decoded['price_fail_streak'] ?? null;
            $lastPriceErr = (string)($decoded['last_price_error'] ?? '');
        } else {
            $lastBeat = $raw;
            $priceFails = null;
            $lastPriceErr = '';
        }
        $ts = strtotime((string)$lastBeat);
        $staleSec = (int)($cfg['paper_trading']['poll_interval'] ?? 15) * 3 + 10;
        $watcherAgeSec = $ts !== false ? time() - $ts : null;
        $watcherRunning = $ts !== false && (time() - $ts) <= max(45, $staleSec);
    } else {
        $priceFails = null;
        $lastPriceErr = '';
    }

    $python = Bridge::pythonBinary();
    $pythonOk = Bridge::pythonAvailable();

    $dbs = [
        'ai_learning' => Database::resolvePath('ai_learning.db'),
        'paper_trading' => Database::resolvePath('paper_trading.db'),
    ];

    $openCount = 0;
    $recentClose = null;
    $pdo = Database::connect('paper_trading.db');
    if ($pdo) {
        $row = Database::row($pdo, "SELECT COUNT(*) AS cnt FROM paper_positions WHERE status='OPEN'");
        $openCount = (int)($row['cnt'] ?? 0);
        $row = Database::row($pdo, "SELECT closed_at FROM paper_positions WHERE status='CLOSED' ORDER BY closed_at DESC LIMIT 1");
        $recentClose = rowVal($row, 'closed_at');
    }

    $aiCount = 0;
    $learningCount = 0;
    $ai = Database::connect('ai_learning.db');
    if ($ai) {
        $row = Database::row($ai, 'SELECT COUNT(*) AS cnt FROM analysis_records');
        $aiCount = (int)($row['cnt'] ?? 0);
        $row = Database::row($ai, 'SELECT COUNT(*) AS cnt FROM learning_log');
        $learningCount = (int)($row['cnt'] ?? 0);
    }

    Response::json([
        'timestamp' => gmdate('c'),
        'watcher' => [
            'running' => $watcherRunning,
            'last_heartbeat' => $lastBeat,
            'age_seconds' => $watcherAgeSec,
            'heartbeat_path' => $hbPath,
            'price_fail_streak' => $priceFails,
            'last_price_error' => $lastPriceErr,
        ],
        'python' => [
            'binary' => $python ?: null,
            'available' => $pythonOk,
            'shell_exec' => shell_available(),
        ],
        'paths' => [
            'project_root' => PROJECT_ROOT,
            'data_dir' => DATA_DIR,
            'config_path' => CONFIG_PATH,
            'config_exists' => is_file(CONFIG_PATH),
            'zip_available' => class_exists('ZipArchive'),
        ],
        'databases' => [
            'ai_learning' => ['path' => $dbs['ai_learning'], 'exists' => $dbs['ai_learning'] !== null],
            'paper_trading' => ['path' => $dbs['paper_trading'], 'exists' => $dbs['paper_trading'] !== null],
        ],
        'services' => [
            'auto_run' => (bool)($cfg['auto_run']['enabled'] ?? false),
            'auto_run_interval_min' => (int)($cfg['auto_run']['interval_minutes'] ?? 0),
            'autopilot_mode' => (string)($cfg['autopilot']['mode'] ?? 'off'),
            'autopilot_interval_min' => (int)($cfg['autopilot']['interval_minutes'] ?? 0),
            'signal_scanner' => (bool)($cfg['signal_scanner']['enabled'] ?? false),
            'paper_auto_follow' => (bool)($cfg['paper_trading']['auto_follow'] ?? false),
            'paper_poll_interval' => (int)($cfg['paper_trading']['poll_interval'] ?? 15),
            'require_gate_pass' => (bool)($cfg['ai_trading']['require_gate_pass'] ?? true),
            'circuit_breaker_enabled' => (bool)($cfg['circuit_breaker']['enabled'] ?? false),
        ],
        'trading' => [
            'symbol' => $cfg['trading']['symbol'] ?? 'BNBUSDT',
            'open_positions' => $openCount,
            'last_close_at' => $recentClose,
            'ai_analysis_count' => $aiCount,
            'learning_log_count' => $learningCount,
        ],
        'security' => [
            'api_token_set' => api_token_configured() !== '',
        ],
        'hints' => [
            'watcher' => $watcherRunning
                ? '模拟盘监控正常'
                : '请运行 paper_watcher.py 或 启动监控.bat',
            'remote' => api_token_configured() !== ''
                ? '已启用 Token 鉴权'
                : '远程暴露前请在 config.yaml 设置 web.api_token',
        ],
    ]);
}

function handleStatus(): void
{
    $cfg = Config::load();
    $hbPath = DATA_DIR . DIRECTORY_SEPARATOR . 'watcher.heartbeat';
    $lastBeat = null;
    $watcherRunning = false;

    if (is_file($hbPath)) {
        $raw = trim((string)file_get_contents($hbPath));
        $decoded = json_decode($raw, true);
        if (is_array($decoded) && !empty($decoded['ts'])) {
            $lastBeat = (string)$decoded['ts'];
        } else {
            $lastBeat = $raw;
        }
        $ts = strtotime((string)$lastBeat);
        $staleSec = (int)($cfg['paper_trading']['poll_interval'] ?? 15) * 3 + 10;
        $watcherRunning = $ts !== false && (time() - $ts) <= max(45, $staleSec);
    }

    $pdo = Database::connect('paper_trading.db');
    $openCount = 0;
    if ($pdo) {
        $row = Database::row($pdo, "SELECT COUNT(*) AS cnt FROM paper_positions WHERE status='OPEN'");
        $openCount = (int)($row['cnt'] ?? 0);
    }

    Response::json([
        'watcher_running' => $watcherRunning,
        'watcher_last_heartbeat' => $lastBeat,
        'open_positions' => $openCount,
        'gui_required_for_watcher' => false,
        'hint' => $watcherRunning
            ? '后台监控运行中'
            : '请运行 paper_watcher.py 或 启动监控.bat 以启用 SL/TP 自动平仓',
    ]);
}

function handleLatestAdvice(): void
{
    $pdo = Database::connect('ai_learning.db');
    if (!$pdo) {
        Response::json(['found' => false]);
    }

    $select = analysisRecordsSelect($pdo);
    $row = Database::row($pdo, "
        SELECT {$select}
        FROM analysis_records
        ORDER BY id DESC
        LIMIT 1
    ");

    if (!$row) {
        Response::json(['found' => false]);
    }

    Response::json(formatAdviceRow($row));
}

function handleDecisionHistory(): void
{
    $limit = max(1, min(50, (int)($_GET['limit'] ?? 10)));
    $pdo = Database::connect('ai_learning.db');
    if (!$pdo) {
        Response::json([]);
    }

    $select = analysisRecordsSelect($pdo);
    $rows = Database::rows($pdo, "
        SELECT {$select}
        FROM analysis_records
        ORDER BY id DESC
        LIMIT ?
    ", [$limit]);

    $out = [];
    foreach ($rows as $row) {
        $item = formatAdviceRow($row);
        $item['found'] = true;
        $out[] = $item;
    }
    Response::json($out);
}

/**
 * 合并 snapshot 与完整 market_regime_json，补全 Web 驾驶舱字段。
 */
function enrichCockpitPayload(?array $snapshot, ?array $regimeFull, array $gateReasons, ?array $multiAgent = null): ?array
{
    // 允许仅有议会数据时也返回 cockpit，避免双模看板被清空
    if (!is_array($snapshot) && !is_array($regimeFull) && !is_array($multiAgent)) {
        return null;
    }
    $cockpit = is_array($snapshot) ? $snapshot : [];
    $mrSnap = is_array($cockpit['market_regime'] ?? null) ? $cockpit['market_regime'] : [];
    $mr = is_array($regimeFull) ? $regimeFull : [];
    foreach ($mrSnap as $key => $val) {
        if ($val === null || $val === '' || (is_array($val) && $val === [])) {
            continue;
        }
        $mr[$key] = $val;
    }
    // snapshot 缺字段时回退完整 regime_json
    $fillKeys = ['regime_votes', 'regime_conflicts', 'fusion_confidence', 'hmm_regime',
        'hmm_confidence', 'hmm_detail', 'hmm_agreement', 'description'];
    if (is_array($regimeFull)) {
        foreach ($fillKeys as $fk) {
            if (empty($mr[$fk]) && !empty($regimeFull[$fk])) {
                $mr[$fk] = $regimeFull[$fk];
            }
        }
        if (empty($mr['regime']) && !empty($regimeFull['regime'])) {
            $mr['regime'] = $regimeFull['regime'];
        }
    }
    $cockpit['market_regime'] = $mr;

    $conv = is_array($cockpit['institutional_conviction'] ?? null)
        ? $cockpit['institutional_conviction'] : [];
    if (!isset($conv['conviction']) && isset($conv['score'])) {
        $conv['conviction'] = $conv['score'];
    }
    if (!isset($conv['score']) && isset($conv['conviction'])) {
        $conv['score'] = $conv['conviction'];
    }
    // 因子补 detail（旧快照可能只有 name/score）
    if (!empty($conv['factors']) && is_array($conv['factors'])) {
        $conv['factors'] = array_values(array_map(static function ($f) {
            if (!is_array($f)) {
                return $f;
            }
            if (!isset($f['detail'])) {
                $f['detail'] = '';
            }
            return $f;
        }, $conv['factors']));
    }
    $cockpit['institutional_conviction'] = $conv;

    if (empty($cockpit['gate_reasons']) && !empty($gateReasons)) {
        $cockpit['gate_reasons'] = $gateReasons;
    }
    if ($multiAgent && empty($cockpit['multi_agent_deliberation'])) {
        $cockpit['multi_agent_deliberation'] = $multiAgent;
    }
    if (empty($cockpit['win_rate_context']) && is_array($snapshot['win_rate_context'] ?? null)) {
        $cockpit['win_rate_context'] = $snapshot['win_rate_context'];
    }

    return $cockpit;
}

function handleRunAnalysis(): void
{
    if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'POST') {
        Response::error('POST required', 405);
        return;
    }
    $body = json_decode(file_get_contents('php://input') ?: '{}', true);
    $openPaper = !isset($body['open_paper']) || (bool)$body['open_paper'];
    $result = Bridge::runAnalysis($openPaper);
    if ($result === null) {
        Response::error('分析脚本不可用或 Python 未配置', 503);
        return;
    }
    Response::json($result);
}

function formatAdviceRow(array $row): array
{
    $explanation = parseJsonField($row['decision_explanation'] ?? null);
    $gateReasons = parseJsonField($row['gate_reasons'] ?? null);
    if (!is_array($gateReasons)) {
        $gateReasons = [];
    }

    $snapshot = parseJsonField($row['trade_advice_snapshot'] ?? null);
    $regimeFull = parseJsonField($row['market_regime_json'] ?? null);
    $multiAgent = parseJsonField($row['multi_agent_deliberation'] ?? null);

    $row['found'] = true;
    $row['risk_passed'] = (bool)($row['risk_passed'] ?? 0);
    $row['passed_gate'] = isset($row['passed_gate']) ? (bool)$row['passed_gate'] : null;
    $row['explanation'] = is_array($explanation) ? $explanation : null;
    $row['gate_reasons'] = $gateReasons;
    $row['has_explanation'] = is_array($explanation) && !empty($explanation['factors']);
    $row['cockpit'] = enrichCockpitPayload(
        is_array($snapshot) ? $snapshot : null,
        is_array($regimeFull) ? $regimeFull : null,
        $gateReasons,
        is_array($multiAgent) ? $multiAgent : null,
    );
    $row['market_regime_full'] = is_array($regimeFull) ? $regimeFull : null;
    // 顶层也挂一份，供前端 mergeCockpitPayload 兜底
    if (is_array($multiAgent)) {
        $row['multi_agent_deliberation'] = $multiAgent;
    }

    $meta = IntelligenceLoop::adviceMeta($row, is_array($snapshot) ? $snapshot : null);
    $row['reused'] = $meta['reused'];
    $row['reuse_reason'] = $meta['reuse_reason'];
    $row['skipped_council'] = $meta['skipped_council'];
    $row['primary_provider'] = $meta['primary_provider'];
    $row['ai_analyses'] = $meta['ai_analyses'];
    $row['synthesis_note'] = $meta['synthesis_note'];

    // 首页「现在」状态条：直接带上执行层字段，避免前端再翻 snapshot
    if (is_array($snapshot)) {
        $row['trade_advice'] = $snapshot;
        if (!empty($snapshot['execution_context']) && is_array($snapshot['execution_context'])) {
            $row['execution_context'] = $snapshot['execution_context'];
        }
        if (!empty($snapshot['learning_phase_probe'])) {
            $row['learning_phase_probe'] = true;
        }
        if (!empty($snapshot['intended_direction'])) {
            $row['intended_direction'] = $snapshot['intended_direction'];
        }
        if (empty($row['raw_action']) && !empty($snapshot['raw_action'])) {
            $row['raw_action'] = $snapshot['raw_action'];
        }
        if (empty($gateReasons) && !empty($snapshot['gate_reasons']) && is_array($snapshot['gate_reasons'])) {
            $row['gate_reasons'] = $snapshot['gate_reasons'];
        }
    }

    unset($row['decision_explanation']);

    return $row;
}

function handleLoopHealth(): void
{
    $ai = Database::connect('ai_learning.db');
    $paper = Database::connect('paper_trading.db');
    $trader = Database::connect('trader_memory.db');
    Response::json(IntelligenceLoop::health($ai, $paper, $trader));
}

function handleCouncilMemory(): void
{
    $limit = max(1, min(30, (int)($_GET['limit'] ?? 12)));
    $trader = Database::connect('trader_memory.db');
    Response::json(IntelligenceLoop::councilMemory($trader, $limit));
}

function handleCircuitBreaker(): void
{
    $method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
    if ($method === 'GET') {
        $py = Bridge::maintenance('circuit_breaker_status', []);
        if ($py !== null && is_array($py)) {
            Response::json($py);
            return;
        }
        Response::json([
            'status' => 'unknown',
            'message' => 'Run GUI or maintenance script for live breaker state',
        ]);
        return;
    }
    if ($method !== 'POST') {
        Response::error('Method not allowed', 405);
    }
    if (!maintenanceEnabled()) {
        Response::error('Maintenance disabled', 403);
    }
    $py = Bridge::maintenance('circuit_breaker_reset', []);
    if ($py !== null && !empty($py['ok'])) {
        Response::json($py);
        return;
    }
    try {
        require_once dirname(__DIR__, 2) . '/includes/bootstrap.php';
        $stateFile = dirname(__DIR__, 3) . '/data/risk_state.json';
        if (is_file($stateFile)) {
            $state = json_decode((string)file_get_contents($stateFile), true) ?: [];
            unset($state['circuit_breaker_stop_ts']);
            file_put_contents($stateFile, json_encode($state, JSON_PRETTY_PRINT));
        }
        Response::json(['ok' => true, 'message' => 'Circuit breaker cooldown cleared']);
    } catch (Throwable $e) {
        Response::error($e->getMessage(), 500);
    }
}

function handleScanSignals(): void
{
    $limit = max(1, min(50, (int)($_GET['limit'] ?? 20)));
    $pdo = Database::connect('paper_trading.db');
    if (!$pdo) {
        Response::json([]);
    }
    $cols = Database::tableColumns($pdo, 'scan_signals');
    if (!$cols) {
        Response::json([]);
    }
    $rows = Database::rows($pdo, "
        SELECT id, signal_type, direction, strength, symbol, price, detail,
               triggered_fullauto, created_at
        FROM scan_signals
        ORDER BY id DESC
        LIMIT ?
    ", [$limit]);
    Response::json($rows);
}

function parseJsonField($value)
{
    if ($value === null || $value === '') {
        return null;
    }
    if (is_array($value)) {
        return $value;
    }
    $decoded = json_decode((string)$value, true);
    return json_last_error() === JSON_ERROR_NONE ? $decoded : null;
}

function maintenanceEnabled(): bool
{
    $cfg = Config::load();
    $upd = $cfg['web']['update'] ?? [];
    return ($upd['enabled'] ?? true) !== false;
}

function callMaintenance(string $action, array $extraArgs = []): array
{
    if ($action === 'backup') {
        $label = (string)($extraArgs[0] ?? 'web');
        return Maintenance::backup($label);
    }
    if ($action === 'backups') {
        $limit = max(1, min(30, (int)($extraArgs[0] ?? 10)));
        return Maintenance::listBackups($limit);
    }
    if ($action === 'optimize') {
        $py = Bridge::maintenance('optimize', $extraArgs);
        if ($py !== null && !empty($py['ok'])) {
            return $py;
        }
        return Maintenance::optimize();
    }
    if ($action === 'fix') {
        $py = Bridge::maintenance('fix', $extraArgs);
        if ($py !== null && !empty($py['ok'])) {
            return $py;
        }
        return Maintenance::autoFix();
    }
    if ($action === 'health') {
        $py = Bridge::maintenance('health', $extraArgs);
        if ($py !== null && is_array($py['checks'] ?? null)) {
            return $py;
        }
        return Maintenance::healthCheck();
    }
    if ($action === 'apply_zip' && isset($extraArgs[0])) {
        $py = Bridge::maintenance('apply_zip', $extraArgs);
        if ($py !== null && !empty($py['ok'])) {
            return $py;
        }
        return Maintenance::applyUpdateZip((string)$extraArgs[0]);
    }

    $result = Bridge::maintenance($action, $extraArgs);
    if ($result !== null) {
        return $result;
    }

    if ($action === 'health' || $action === 'status') {
        $fallback = Bridge::maintenanceFallbackHealth();
        if ($action === 'status') {
            return array_merge($fallback, [
                'version' => 'unknown',
                'git' => ['available' => false],
                'project_root' => PROJECT_ROOT,
                'python_available' => Bridge::pythonAvailable(),
            ]);
        }
        return $fallback;
    }

    return [
        'ok' => false,
        'error' => Bridge::pythonAvailable()
            ? '维护脚本执行失败，请检查 web/scripts/maintenance.py'
            : '需要 Python：请在 config.yaml 设置 web.python_path',
    ];
}

function handleMaintenance(): void
{
    $sub = $_GET['action'] ?? 'status';
    $input = [];

    if ($_SERVER['REQUEST_METHOD'] === 'POST') {
        require_write_token();
        if (!maintenanceEnabled()) {
            Response::error('维护功能已禁用', 403);
        }
        $input = json_decode(file_get_contents('php://input') ?: '{}', true);
        if (!is_array($input)) {
            $input = $_POST;
        }
        $sub = (string)($input['action'] ?? $sub);
    }

    $allowedGet = ['status', 'health', 'backups'];
    if ($_SERVER['REQUEST_METHOD'] === 'GET' && !in_array($sub, $allowedGet, true)) {
        Response::error('GET 仅支持 status / health / backups', 405);
    }

    $writeActions = ['fix', 'optimize', 'backup', 'git_pull'];
    if (in_array($sub, $writeActions, true) && $_SERVER['REQUEST_METHOD'] !== 'POST') {
        Response::error('POST required', 405);
    }
    if ($_SERVER['REQUEST_METHOD'] === 'POST' && !in_array($sub, $writeActions, true) && $sub !== 'status' && $sub !== 'health' && $sub !== 'backups') {
        Response::error('Unknown maintenance action: ' . $sub, 400);
    }

    $extra = [];
    if ($sub === 'backups') {
        $extra[] = (string)max(1, min(30, (int)($_GET['limit'] ?? ($input['limit'] ?? 10))));
    }
    if ($sub === 'backup') {
        $label = preg_replace('/[^a-zA-Z0-9_-]/', '', (string)($input['label'] ?? 'web'));
        $extra[] = $label !== '' ? $label : 'web';
    }

    respondMaintenance($sub, $extra);
}

function respondMaintenance(string $sub, array $extra): void
{
    $hardFailActions = ['backup', 'optimize', 'git_pull'];
    $result = callMaintenance($sub, $extra);
    if (empty($result['ok']) && in_array($sub, $hardFailActions, true)) {
        Response::error((string)($result['error'] ?? '操作失败'), 400);
    }
    Response::json($result);
}

function handleMaintenanceUpload(): void
{
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        Response::error('POST required', 405);
    }
    require_write_token();
    if (!maintenanceEnabled()) {
        Response::error('维护功能已禁用', 403);
    }

    if (empty($_FILES['package']) || ($_FILES['package']['error'] ?? UPLOAD_ERR_NO_FILE) !== UPLOAD_ERR_OK) {
        Response::error('请上传 zip 更新包 (字段名 package)');
    }

    $tmp = $_FILES['package']['tmp_name'];
    $name = $_FILES['package']['name'] ?? 'update.zip';
    if (!is_uploaded_file($tmp)) {
        Response::error('无效的上传文件');
    }

    $finfo = finfo_open(FILEINFO_MIME_TYPE);
    $mime = $finfo ? finfo_file($finfo, $tmp) : '';
    if ($finfo) {
        finfo_close($finfo);
    }
    $ext = strtolower(pathinfo($name, PATHINFO_EXTENSION));
    if ($ext !== 'zip') {
        Response::error('仅支持 .zip 文件');
    }

    $updatesDir = DATA_DIR . DIRECTORY_SEPARATOR . 'updates';
    if (!is_dir($updatesDir)) {
        mkdir($updatesDir, 0755, true);
    }
    $dest = $updatesDir . DIRECTORY_SEPARATOR . 'upload_' . date('Ymd_His') . '.zip';
    if (!move_uploaded_file($tmp, $dest)) {
        Response::error('保存上传文件失败');
    }

    $result = callMaintenance('apply_zip', [$dest]);
    if (!$result['ok']) {
        Response::json($result, 400);
    }
    $result['upload'] = basename($dest);
    Response::json($result);
}
