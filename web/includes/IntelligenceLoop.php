<?php
declare(strict_types=1);

/**
 * 感知→决策→执行→反思→记忆 — Web 侧闭环健康度与议会记忆。
 * 与 Python intelligence_loop.get_loop_health 对齐（只读 SQLite，不调 Python）。
 */
class IntelligenceLoop
{
    private const STAGES = [
        ['id' => 'perceive', 'label' => '感知'],
        ['id' => 'decide', 'label' => '决策'],
        ['id' => 'execute', 'label' => '执行'],
        ['id' => 'reflect', 'label' => '反思'],
        ['id' => 'memory', 'label' => '记忆'],
    ];

    /** @return array<string, mixed> */
    public static function health(?PDO $ai, ?PDO $paper, ?PDO $trader): array
    {
        $cfg = Config::load();
        $loopCfg = is_array($cfg['intelligence_loop'] ?? null) ? $cfg['intelligence_loop'] : [];
        $enabled = ($loopCfg['enabled'] ?? true) !== false;

        $health = [
            'enabled' => $enabled,
            'memory_driven' => false,
            'stages' => self::STAGES,
            'symbol' => (string)($cfg['trading']['symbol'] ?? 'BNBUSDT'),
            'checked_at' => gmdate('c'),
            'total_analyses' => 0,
            'total_feedbacks' => 0,
            'overall_accuracy' => 0.0,
            'learning_maturity' => 'BEGINNER',
            'knowledge_cards' => 0,
            'paper_closed' => 0,
            'paper_win_rate' => 0.0,
            'council_outcome_samples' => 0,
            'council_vote_samples' => 0,
            'completeness_score' => 0,
            'completeness_label' => '雏形',
            'llm' => self::llmSummary($cfg),
            'reuse' => self::reuseSummary($cfg),
            'autopilot' => [
                'mode' => (string)($cfg['autopilot']['mode'] ?? 'off'),
                'follow_ai_direction' => (bool)($cfg['ai_trading']['follow_ai_direction'] ?? false),
                'require_gate_pass' => (bool)($cfg['ai_trading']['require_gate_pass'] ?? true),
                'open_paper_on_fullauto' => (bool)($cfg['autopilot']['open_paper_on_fullauto'] ?? true),
            ],
        ];

        if (!$enabled) {
            $health['error'] = 'disabled';
            return $health;
        }

        if ($ai) {
            $health['memory_driven'] = true;
            $health['total_analyses'] = self::scalar($ai, 'SELECT COUNT(*) AS c FROM analysis_records');
            $health['total_feedbacks'] = self::scalar(
                $ai,
                'SELECT COUNT(*) AS c FROM analysis_records WHERE actual_result IS NOT NULL'
            );
            $health['knowledge_cards'] = self::countKnowledgeCards($ai);
            $fb = $health['total_feedbacks'];
            if ($fb >= 50) {
                $health['learning_maturity'] = 'EXPERT';
            } elseif ($fb >= 20) {
                $health['learning_maturity'] = 'ADVANCED';
            } elseif ($fb >= 5) {
                $health['learning_maturity'] = 'INTERMEDIATE';
            }
            $wins = self::scalar(
                $ai,
                "SELECT COUNT(*) AS c FROM analysis_records WHERE actual_result IN ('WIN','CORRECT')"
            );
            if ($fb > 0) {
                $health['overall_accuracy'] = round($wins / $fb, 4);
            }
        }

        if ($paper) {
            $row = Database::row(
                $paper,
                "SELECT COUNT(*) AS total,
                        SUM(CASE WHEN COALESCE(realized_pnl_usdt,0) > 0 THEN 1 ELSE 0 END) AS wins
                 FROM paper_positions WHERE status='CLOSED'"
            );
            $closed = (int)($row['total'] ?? 0);
            $health['paper_closed'] = $closed;
            if ($closed > 0) {
                $health['paper_win_rate'] = round(((int)($row['wins'] ?? 0)) / $closed, 4);
            }
        }

        if ($trader) {
            $health['council_outcome_samples'] = self::scalar(
                $trader,
                'SELECT COUNT(*) AS c FROM trader_outcomes'
            );
            $health['council_vote_samples'] = self::scalar(
                $trader,
                'SELECT COUNT(*) AS c FROM trader_votes'
            );
        }

        $score = 0;
        if ($health['total_feedbacks'] >= 3) {
            $score += 25;
        }
        if ($health['knowledge_cards'] >= 5) {
            $score += 25;
        }
        if ($health['paper_closed'] >= 5) {
            $score += 25;
        }
        if ($health['council_outcome_samples'] >= 3) {
            $score += 25;
        }
        $health['completeness_score'] = $score;
        $health['completeness_label'] = $score >= 75
            ? '完整'
            : ($score >= 50 ? '成形' : ($score >= 25 ? '起步' : '雏形'));

        return $health;
    }

    /**
     * 议会交易员准确率 / 教训摘要。
     * @return array{traders: array<int, array<string, mixed>>, top_lessons: string[], sample_traders: int}
     */
    public static function councilMemory(?PDO $trader, int $limit = 12): array
    {
        $empty = ['traders' => [], 'top_lessons' => [], 'sample_traders' => 0];
        if (!$trader) {
            return $empty;
        }

        try {
            $rows = Database::rows($trader, "
                SELECT trader_id,
                       COUNT(*) AS total,
                       SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) AS wins
                FROM trader_outcomes
                GROUP BY trader_id
                ORDER BY total DESC
                LIMIT ?
            ", [$limit]);
        } catch (Throwable $e) {
            return $empty;
        }

        $traders = [];
        $lessons = [];
        foreach ($rows as $row) {
            $tid = (string)($row['trader_id'] ?? '');
            if ($tid === '') {
                continue;
            }
            $total = (int)($row['total'] ?? 0);
            $wins = (int)($row['wins'] ?? 0);
            $acc = $total > 0 ? round($wins / $total, 4) : 0.5;
            $weight = round(0.5 + $acc, 3);
            $lesson = self::traderLesson($trader, $tid);
            $traders[] = [
                'trader_id' => $tid,
                'name' => self::traderLabel($tid),
                'total' => $total,
                'wins' => $wins,
                'accuracy' => $acc,
                'weight' => $weight,
                'lesson' => $lesson,
            ];
            if ($lesson !== '' && $total > 0) {
                $line = explode("\n", $lesson)[0];
                $lessons[] = $tid . ': ' . substr($line, 0, 100);
            }
        }

        return [
            'traders' => $traders,
            'top_lessons' => array_slice($lessons, 0, 6),
            'sample_traders' => count(array_filter($traders, static fn ($t) => ($t['total'] ?? 0) > 0)),
        ];
    }

    /** @return array<string, mixed> */
    public static function llmSummary(array $cfg): array
    {
        $llm = is_array($cfg['llm'] ?? null) ? $cfg['llm'] : [];
        $providers = $llm['analyzer_providers'] ?? $llm['dual_providers'] ?? [];
        if (!is_array($providers)) {
            $providers = [];
        }
        $providers = array_values(array_filter(array_map('strval', $providers)));
        return [
            'mode' => (string)($llm['mode'] ?? $llm['provider'] ?? 'single'),
            'analyzer_provider' => (string)($llm['analyzer_provider'] ?? ''),
            'providers' => $providers,
            'synthesis' => (bool)($llm['synthesis'] ?? false),
            'synthesis_min_agree' => (int)($llm['synthesis_min_agree'] ?? 2),
            'council_providers' => array_values(array_filter(array_map(
                'strval',
                is_array($llm['council_providers'] ?? null) ? $llm['council_providers'] : []
            ))),
        ];
    }

    /** @return array<string, mixed> */
    public static function reuseSummary(array $cfg): array
    {
        $cm = is_array($cfg['capability_memory'] ?? null) ? $cfg['capability_memory'] : [];
        $actions = $cm['reuse_actions'] ?? [];
        if (!is_array($actions)) {
            $actions = [];
        }
        return [
            'enabled' => (bool)($cm['reuse_known_situation'] ?? false),
            'skip_llm' => (bool)($cm['skip_llm_on_reuse'] ?? true),
            'skip_council' => (bool)($cm['skip_council_on_reuse'] ?? true),
            'require_win' => (bool)($cm['reuse_trade_require_win'] ?? false),
            'actions' => array_values(array_map('strval', $actions)),
            'ttl_minutes' => (int)($cm['reuse_ttl_minutes'] ?? 0),
        ];
    }

    /**
     * 从分析行 / snapshot 提取复用与多模型元数据。
     * @param array<string, mixed> $row
     * @param array<string, mixed>|null $snapshot
     * @return array<string, mixed>
     */
    public static function adviceMeta(array $row, ?array $snapshot): array
    {
        $snap = is_array($snapshot) ? $snapshot : [];
        $ai = [];
        if (isset($snap['ai_analysis']) && is_array($snap['ai_analysis'])) {
            $ai = $snap['ai_analysis'];
        }
        $reused = (bool)(
            ($ai['_reused'] ?? false)
            || ($snap['_reused'] ?? false)
            || (($ai['_provider'] ?? '') === 'knowledge_reuse')
            || ($snap['_skipped_council_reuse'] ?? false)
        );
        $analyses = [];
        if (isset($snap['ai_analyses']) && is_array($snap['ai_analyses'])) {
            foreach ($snap['ai_analyses'] as $prov => $pack) {
                if (!is_array($pack)) {
                    continue;
                }
                $analyses[(string)$prov] = [
                    'signal' => (string)($pack['signal'] ?? $pack['trade_suggestion'] ?? ''),
                    'confidence' => (float)($pack['confidence'] ?? 0),
                    'degraded' => (bool)($pack['_degraded'] ?? $pack['_error'] ?? false),
                ];
            }
        }
        return [
            'reused' => $reused,
            'reuse_reason' => (string)(
                $ai['_reuse_reason'] ?? $snap['_reuse_reason'] ?? ($reused ? '知识复用' : '')
            ),
            'skipped_council' => (bool)($snap['_skipped_council_reuse'] ?? false),
            'primary_provider' => (string)(
                $snap['ai_primary_provider'] ?? $ai['_provider'] ?? ''
            ),
            'ai_analyses' => $analyses,
            'synthesis_note' => (string)($snap['ai_analysis_note'] ?? $ai['_synthesis_note'] ?? ''),
        ];
    }

    private static function scalar(PDO $pdo, string $sql): int
    {
        try {
            $row = Database::row($pdo, $sql);
            return (int)($row['c'] ?? $row['cnt'] ?? 0);
        } catch (Throwable $e) {
            return 0;
        }
    }

    private static function countKnowledgeCards(PDO $ai): int
    {
        $g = AiGrowth::growthSnapshot($ai);
        return (int)($g['knowledge_cards'] ?? 0);
    }

    private static function traderLesson(PDO $pdo, string $traderId): string
    {
        try {
            $row = Database::row(
                $pdo,
                'SELECT lessons FROM trader_notes WHERE trader_id=? LIMIT 1',
                [$traderId]
            );
            if ($row && !empty($row['lessons'])) {
                return trim((string)$row['lessons']);
            }
            // persona__provider → 基座 ID
            if (str_contains($traderId, '__')) {
                $base = explode('__', $traderId, 2)[0];
                $row = Database::row(
                    $pdo,
                    'SELECT lessons FROM trader_notes WHERE trader_id=? LIMIT 1',
                    [$base]
                );
                if ($row && !empty($row['lessons'])) {
                    return trim((string)$row['lessons']);
                }
            }
        } catch (Throwable $e) {
            return '';
        }
        return '';
    }

    private static function traderLabel(string $tid): string
    {
        $base = str_contains($tid, '__') ? explode('__', $tid, 2)[0] : $tid;
        $map = [
            'momentum' => '趋势猎手',
            'mean_reversion' => '均值回归',
            'macro' => '宏观情绪',
            'structure' => '结构派',
            'flow' => '资金流',
            'contrarian' => '反共识',
        ];
        $name = $map[$base] ?? $base;
        if (str_contains($tid, '__')) {
            $prov = explode('__', $tid, 2)[1];
            $name .= ' · ' . $prov;
        }
        return $name;
    }
}
