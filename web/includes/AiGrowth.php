<?php
declare(strict_types=1);

/**
 * AI 能力成长与策略贡献度 — 与 Python AILearningSystem.get_growth_snapshot 对齐（含五维能力模型）。
 */
class AiGrowth
{
    /** @return array<string, mixed> */
    public static function growthSnapshot(?PDO $ai): array
    {
        $emptyDims = self::emptyDimensions();
        $empty = [
            'analysis_count' => 0,
            'feedback_count' => 0,
            'knowledge_cards' => 0,
            'validated_knowledge_cards' => 0,
            'pattern_memory_count' => 0,
            'weight_optimizations' => 0,
            'growth_events' => 0,
            'learning_maturity' => 'BEGINNER',
            'capability_level' => 0,
            'capability_dimensions' => $emptyDims,
            'paper_win_rate' => 0.0,
            'avg_quality_score' => 50.0,
        ];
        if (!$ai) {
            return $empty;
        }

        $totalAnalyses = self::scalar($ai, 'SELECT COUNT(*) AS cnt FROM analysis_records');
        $totalFeedbacks = self::scalar(
            $ai,
            'SELECT COUNT(*) AS cnt FROM analysis_records WHERE actual_result IS NOT NULL'
        );
        $weightOpts = self::scalar(
            $ai,
            "SELECT COUNT(*) AS cnt FROM learning_log WHERE event_type='OPTIMIZATION'"
        );
        $growthEvents = self::scalar(
            $ai,
            "SELECT COUNT(*) AS cnt FROM learning_log WHERE event_type='GROWTH'"
        );
        $knowledgeCards = self::countKnowledgeCards($ai);
        $validatedCards = self::countValidatedKnowledgeCards($ai);
        $patternCount = self::countPatternMemory();
        $avgQuality = self::avgFeedbackQualityScore($ai);
        $gatePassRate = self::gatePassRate($ai);
        $circuitRate = self::circuitTriggerRate($ai, $totalAnalyses);
        $metaRuns = self::metaLearningCount($ai);
        $paperWr = self::paperWinRate();

        $maturity = self::resolveMaturity($totalFeedbacks);

        $sampleMaturity = min(100, (int) round($totalFeedbacks * 2 + $totalAnalyses * 0.1));
        $predictionAccuracy = min(100, (int) round($paperWr * 100));
        $knowledgeQuality = min(
            100,
            (int) round($validatedCards * 8 + $knowledgeCards * 0.5 + $avgQuality * 0.3)
        );
        $discipline = min(100, (int) round($gatePassRate * 80 + (100 - $circuitRate)));
        $evolutionActivity = min(
            100,
            (int) round($weightOpts * 5 + $metaRuns * 10 + $growthEvents * 2)
        );

        $capabilityDimensions = [
            'sample_maturity' => $sampleMaturity,
            'prediction_accuracy' => $predictionAccuracy,
            'knowledge_quality' => $knowledgeQuality,
            'discipline' => $discipline,
            'evolution_activity' => $evolutionActivity,
        ];

        $capabilityLevel = min(
            100,
            (int) round(
                $sampleMaturity * 0.20
                + $predictionAccuracy * 0.30
                + $knowledgeQuality * 0.25
                + $discipline * 0.15
                + $evolutionActivity * 0.10
            )
        );

        $minFeedback = 10;
        $maxWithoutFeedback = 25;
        if ($totalFeedbacks < $minFeedback) {
            $capabilityLevel = min($capabilityLevel, $maxWithoutFeedback);
        }

        return [
            'analysis_count' => $totalAnalyses,
            'feedback_count' => $totalFeedbacks,
            'knowledge_cards' => $knowledgeCards,
            'validated_knowledge_cards' => $validatedCards,
            'pattern_memory_count' => $patternCount,
            'weight_optimizations' => $weightOpts,
            'growth_events' => $growthEvents,
            'learning_maturity' => $maturity,
            'capability_level' => $capabilityLevel,
            'capability_dimensions' => $capabilityDimensions,
            'paper_win_rate' => round($paperWr, 4),
            'avg_quality_score' => round($avgQuality, 1),
        ];
    }

    /** @return array<string, int> */
    private static function emptyDimensions(): array
    {
        return [
            'sample_maturity' => 0,
            'prediction_accuracy' => 0,
            'knowledge_quality' => 0,
            'discipline' => 0,
            'evolution_activity' => 0,
        ];
    }

    private static function resolveMaturity(int $totalFeedbacks): string
    {
        if ($totalFeedbacks >= 100) {
            return 'EXPERT';
        }
        if ($totalFeedbacks >= 50) {
            return 'ADVANCED';
        }
        if ($totalFeedbacks >= 20) {
            return 'INTERMEDIATE';
        }
        if ($totalFeedbacks >= 10) {
            return 'BEGINNER';
        }
        return 'BEGINNER';
    }

    /**
     * 策略贡献度：投票权重占比 × 历史胜率（样本不足时降权）。
     *
     * @return list<array<string, mixed>>
     */
    public static function strategyContribution(?PDO $ai, int $limit = 8): array
    {
        if (!$ai || $limit <= 0) {
            return [];
        }

        try {
            $rows = Database::rows($ai, "
                SELECT strategy_name, total_predictions, correct_predictions,
                       win_rate, weight, is_active, streak_current, last_updated,
                       weighted_correct
                FROM strategy_performance
                WHERE is_active = 1
                  AND strategy_name NOT LIKE 'paper_%'
                ORDER BY weight DESC, win_rate DESC
            ");
        } catch (Throwable $e) {
            return [];
        }

        if (!$rows) {
            return [];
        }

        $weightSum = 0.0;
        foreach ($rows as $row) {
            $weightSum += max(0.0, (float)($row['weight'] ?? 0));
        }
        if ($weightSum <= 0) {
            $weightSum = (float) count($rows);
            foreach ($rows as &$row) {
                $row['weight'] = 1.0 / count($rows);
            }
            unset($row);
        }

        $out = [];
        foreach ($rows as $row) {
            $weight = max(0.0, (float)($row['weight'] ?? 0));
            $winRate = (float)($row['win_rate'] ?? 0);
            $samples = (int)($row['total_predictions'] ?? 0);
            $weighted = (float)($row['weighted_correct'] ?? 0);
            $effectiveWr = $samples > 0 && $weighted > 0
                ? $weighted / $samples
                : max(0.05, $winRate);
            $contribPct = round(($weight / $weightSum) * 100, 2);
            $sampleFactor = min(1.0, $samples / 20.0);
            $impact = round($contribPct * max(0.05, $effectiveWr) * $sampleFactor, 2);

            $out[] = [
                'strategy_name' => (string)$row['strategy_name'],
                'total_predictions' => $samples,
                'correct_predictions' => (int)($row['correct_predictions'] ?? 0),
                'win_rate' => round($winRate, 4),
                'effective_win_rate' => round($effectiveWr, 4),
                'weight' => round($weight, 6),
                'contribution_pct' => $contribPct,
                'impact_score' => $impact,
                'streak_current' => (int)($row['streak_current'] ?? 0),
                'last_updated' => $row['last_updated'] ?? null,
                'sample_factor' => round($sampleFactor, 2),
            ];
        }

        usort($out, static function (array $a, array $b): int {
            return $b['impact_score'] <=> $a['impact_score']
                ?: $b['contribution_pct'] <=> $a['contribution_pct'];
        });

        return array_slice($out, 0, $limit);
    }

    private static function scalar(PDO $pdo, string $sql): int
    {
        try {
            $row = Database::row($pdo, $sql);
            return (int)($row['cnt'] ?? 0);
        } catch (Throwable $e) {
            return 0;
        }
    }

    private static function scalarFloat(PDO $pdo, string $sql): float
    {
        try {
            $row = Database::row($pdo, $sql);
            return (float)($row['val'] ?? 0);
        } catch (Throwable $e) {
            return 0.0;
        }
    }

    private static function countKnowledgeCards(PDO $ai): int
    {
        try {
            return self::scalar(
                $ai,
                'SELECT COUNT(*) AS cnt FROM knowledge_cards WHERE is_active=1'
            );
        } catch (Throwable $e) {
            return 0;
        }
    }

    private static function countValidatedKnowledgeCards(PDO $ai): int
    {
        try {
            return self::scalar(
                $ai,
                'SELECT COUNT(*) AS cnt FROM knowledge_cards '
                . 'WHERE is_active=1 AND times_validated > 0'
            );
        } catch (Throwable $e) {
            return 0;
        }
    }

    private static function avgFeedbackQualityScore(PDO $ai): float
    {
        try {
            $val = self::scalarFloat(
                $ai,
                'SELECT AVG(quality_score) AS val FROM analysis_records '
                . 'WHERE quality_score IS NOT NULL AND actual_result IS NOT NULL'
            );
            return $val > 0 ? $val : 50.0;
        } catch (Throwable $e) {
            return 50.0;
        }
    }

    private static function gatePassRate(PDO $ai): float
    {
        try {
            return self::scalarFloat(
                $ai,
                'SELECT AVG(CASE WHEN passed_gate=1 THEN 1.0 ELSE 0.0 END) AS val '
                . 'FROM analysis_records'
            );
        } catch (Throwable $e) {
            return 0.5;
        }
    }

    private static function circuitTriggerRate(PDO $ai, int $totalAnalyses): float
    {
        if ($totalAnalyses <= 0) {
            return 0.0;
        }
        try {
            $blocked = self::scalar(
                $ai,
                "SELECT COUNT(*) AS cnt FROM learning_log WHERE event_type='CIRCUIT_BREAKER'"
            );
            return min(100.0, ($blocked / $totalAnalyses) * 100);
        } catch (Throwable $e) {
            return 0.0;
        }
    }

    private static function metaLearningCount(PDO $ai): int
    {
        try {
            return self::scalar($ai, 'SELECT COUNT(*) AS cnt FROM meta_learning_log');
        } catch (Throwable $e) {
            return 0;
        }
    }

    private static function paperWinRate(): float
    {
        $pdo = Database::connect('paper_trading.db');
        if (!$pdo) {
            return 0.0;
        }
        try {
            $row = Database::row($pdo, "
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN realized_pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins
                FROM paper_positions
                WHERE status='CLOSED' AND r_multiple IS NOT NULL
            ");
            $total = (int)($row['total'] ?? 0);
            $wins = (int)($row['wins'] ?? 0);
            return $total > 0 ? $wins / $total : 0.0;
        } catch (Throwable $e) {
            return 0.0;
        }
    }

    private static function countPatternMemory(): int
    {
        $pdo = Database::connect('pattern_memory.db');
        if (!$pdo) {
            return 0;
        }
        try {
            return self::scalar($pdo, 'SELECT COUNT(*) AS cnt FROM pattern_fingerprints');
        } catch (Throwable $e) {
            return 0;
        }
    }

    /** @return list<array<string, mixed>> */
    public static function factorAttribution(?PDO $ai, int $limit = 12): array
    {
        if (!$ai || $limit <= 0) {
            return [];
        }
        try {
            $rows = Database::rows($ai, "
                SELECT factor_key, regime, wins, losses,
                       net_score_when_win, net_score_when_loss
                FROM factor_attribution
                ORDER BY (wins + losses) DESC, wins DESC
                LIMIT ?
            ", [$limit]);
        } catch (Throwable $e) {
            return [];
        }
        $out = [];
        foreach ($rows as $row) {
            $wins = (int)($row['wins'] ?? 0);
            $losses = (int)($row['losses'] ?? 0);
            $total = $wins + $losses;
            $wr = $total > 0 ? $wins / $total : 0.0;
            $tag = $wr >= 0.6 ? 'reliable' : ($wr < 0.4 ? 'caution' : 'neutral');
            $out[] = [
                'factor_key' => (string)($row['factor_key'] ?? ''),
                'regime' => (string)($row['regime'] ?? 'GLOBAL'),
                'wins' => $wins,
                'losses' => $losses,
                'win_rate' => round($wr, 4),
                'tag' => $tag,
            ];
        }
        return $out;
    }

    /** @return list<array<string, mixed>> */
    public static function shadowParamTrials(?PDO $ai, int $limit = 10): array
    {
        if (!$ai || $limit <= 0) {
            return [];
        }
        try {
            $rows = Database::rows($ai, "
                SELECT id, param_name, baseline_value, shadow_value, status,
                       trades_observed, baseline_wins, shadow_wins, reason, timestamp
                FROM shadow_param_trials
                ORDER BY id DESC
                LIMIT ?
            ", [$limit]);
        } catch (Throwable $e) {
            return [];
        }
        $out = [];
        foreach ($rows as $row) {
            $tid = (int)($row['id'] ?? 0);
            $gate = ['b' => 0, 's' => 0, 'n' => 0];
            try {
                $g = Database::row($ai, "
                    SELECT SUM(baseline_would_open) AS b,
                           SUM(shadow_would_open) AS s,
                           COUNT(*) AS n
                    FROM shadow_gate_decisions WHERE trial_id=?
                ", [$tid]);
                $gate = [
                    'b' => (int)($g['b'] ?? 0),
                    's' => (int)($g['s'] ?? 0),
                    'n' => (int)($g['n'] ?? 0),
                ];
            } catch (Throwable $e) {
                // shadow_gate_decisions may not exist yet
            }
            $out[] = [
                'id' => $tid,
                'param_name' => (string)($row['param_name'] ?? ''),
                'baseline_value' => (float)($row['baseline_value'] ?? 0),
                'shadow_value' => (float)($row['shadow_value'] ?? 0),
                'status' => (string)($row['status'] ?? 'active'),
                'trades_observed' => (int)($row['trades_observed'] ?? 0),
                'gate_baseline_opens' => $gate['b'],
                'gate_shadow_opens' => $gate['s'],
                'gate_decisions' => $gate['n'],
                'reason' => (string)($row['reason'] ?? ''),
                'timestamp' => $row['timestamp'] ?? null,
            ];
        }
        return $out;
    }

    /** @return list<array<string, mixed>> */
    public static function agentAccuracy(?PDO $ai): array
    {
        if (!$ai) {
            return [];
        }
        try {
            $rows = Database::rows($ai, "
                SELECT agent_role, correct_predictions, total_predictions,
                       accuracy, last_updated
                FROM agent_accuracy
                ORDER BY total_predictions DESC
            ");
        } catch (Throwable $e) {
            return [];
        }
        $out = [];
        foreach ($rows as $row) {
            $out[] = [
                'agent_role' => (string)($row['agent_role'] ?? ''),
                'correct' => (int)($row['correct_predictions'] ?? 0),
                'total' => (int)($row['total_predictions'] ?? 0),
                'accuracy' => round((float)($row['accuracy'] ?? 0.5), 4),
                'last_updated' => $row['last_updated'] ?? null,
            ];
        }
        return $out;
    }
}
